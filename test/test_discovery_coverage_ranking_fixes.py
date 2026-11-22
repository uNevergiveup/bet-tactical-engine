#!/usr/bin/env python3
"""Pruebas obligatorias de descubrimiento, cobertura por familia, robustez, Bwin manual y presupuestos."""
from __future__ import annotations

import copy
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bwin_manual_links as bml
import filter_major_leagues as fml
import prematch_daily
import prematch_daily_csv
import prematch_prospective_trial as trial
import prematch_runtime
import prematch_tactical_signal_engine as tse
import preselect_matches as pm
import rank_matches as rm
from prematch_audit import CandidateLedger

ROOT = Path(__file__).resolve().parent.parent
REAL_18_EVAL = ROOT / "outputs/discovery/2026-11-18_1500_0300/evaluations/20261118T134610142084Z_c515d959b536"


def _make_obs(fid_base: int, team_id: int, n: int, now: datetime, *, goals: float = 2.0, corners: float | None = 5.0, jitter: float = 0.1) -> list[dict]:
    rows = []
    for i in range(n):
        v = goals + (i % 2) * jitter
        own = {"goals": v}
        conceded = {"goals": v}
        if corners is not None:
            own["corners"] = corners + (i % 2) * 0.2
            conceded["corners"] = corners + (i % 2) * 0.2
        rows.append({
            "fixture_id": fid_base + i,
            "team_id": team_id,
            "opponent_id": 8000 + i,
            "kickoff_utc": (now - timedelta(days=i + 2)).isoformat(),
            "league_id": 71,
            "season": 2026,
            "venue": "HOME" if i % 2 == 0 else "AWAY",
            "status": "FT",
            "statistics_requested": corners is not None,
            "own": own,
            "conceded": conceded,
        })
    return rows


class TestDiscoveryCoverageRankingFixes(unittest.TestCase):
    def test_00_adult_open_keeps_stable_european_top_tiers_without_season_entry(self) -> None:
        base = {
            "season": 2026,
            "status": "NS",
            "kickoff_madrid": "2026-11-21T20:45:00+01:00",
        }
        cases = (
            ({**base, "fixture_id": 1, "country": "France", "league": "Ligue 1", "league_id": 61,
              "home": "Lens", "away": "Toulouse"}, "M"),
            ({**base, "fixture_id": 2, "country": "Germany", "league": "Bundesliga", "league_id": 78,
              "home": "Union Berlin", "away": "RB Leipzig"}, "M"),
            ({**base, "fixture_id": 3, "country": "Italy", "league": "Serie A", "league_id": 135,
              "home": "Napoli", "away": "Torino"}, "M"),
        )
        document = {
            "schema_version": "1.0-discovery",
            "generated_at_utc": "2026-11-21T00:00:00+00:00",
            "fixtures": [row for row, _ in cases],
        }
        result = fml.filter_document(document, json.dumps(document).encode(), policy="adult-open")
        self.assertEqual(result["counts"]["eligible"], 3)
        self.assertEqual(result["counts"]["review"], 0)
        by_id = {row["fixture_id"]: row for row in result["fixtures"]}
        for row, expected_gender in cases:
            admitted = by_id[row["fixture_id"]]
            self.assertEqual(admitted["filter_reason"], "CURATED_STABLE_DOMESTIC_TOP_TIER")
            self.assertEqual(admitted["competition_category"], "SENIOR_MALE_TOP_TIER")
            self.assertEqual(admitted["gender"], expected_gender)
            self.assertEqual(admitted["matching_eligibility"], "ELIGIBLE")

        # La precedencia de los bloqueos de categoria no cambia.
        reserve = {**cases[1][0], "fixture_id": 4, "home": "Bayern München II"}
        youth = {**cases[2][0], "fixture_id": 5, "league": "Serie A U20"}
        self.assertEqual(fml.classify(reserve, policy="adult-open"), ("EXCLUDED", "RESERVE_TEAM"))
        self.assertEqual(fml.classify(youth, policy="adult-open"), ("EXCLUDED", "YOUTH_COMPETITION"))

    def test_01_replay_18_nov_98_fixtures_categories_and_scopes(self) -> None:
        fx_path = REAL_18_EVAL / "fixtures.json"
        if not fx_path.is_file():
            self.skipTest("Snapshot real del 18/11/2026 no disponible")
        raw = fx_path.read_bytes()
        data = json.loads(raw)
        self.assertEqual(len(data["fixtures"]), 98)

        # A) domestic-top-tier: mantiene compatibilidad sin etiquetar Champions Women 525 como YOUTH_RESERVES_OR_CUP
        res_dom = fml.filter_document(data, raw, policy="domestic-top-tier")
        self.assertEqual(res_dom["counts"]["eligible"], 11)
        all_dom = {r["fixture_id"]: r for r in res_dom["fixtures"] + res_dom["review"] + res_dom["excluded"]}
        self.assertEqual(all_dom[1638306]["league_id"], 525)
        self.assertEqual(all_dom[1638306]["filter_status"], "EXCLUDED")
        self.assertEqual(all_dom[1638306]["filter_reason"], "CUP_OR_INTERNATIONAL_EXCLUDED_BY_DOMESTIC_TOP_TIER")
        self.assertEqual(all_dom[1638306]["competition_category"], "SENIOR_ELIGIBLE_CUP_OR_INTERNATIONAL")
        self.assertEqual(all_dom[1638306]["gender"], "F")
        # Filial Dinamo Minsk II vs BATE II excluido por evidencia verificable, no en REVIEW
        self.assertEqual(all_dom[1534466]["filter_status"], "EXCLUDED")
        self.assertEqual(all_dom[1534466]["competition_category"], "RESERVES")

        # B) top-tier-and-senior-cups: admite primeras divisiones M/F + Champions Women (525) + Copa del Rey (143)
        res_top = fml.filter_document(data, raw, policy="top-tier-and-senior-cups")
        self.assertEqual(res_top["counts"]["eligible"], 21)
        self.assertEqual(res_top["counts"]["eligible_female"], 4)
        all_top = {r["fixture_id"]: r for r in res_top["fixtures"] + res_top["review"] + res_top["excluded"]}
        self.assertEqual(all_top[1638306]["filter_status"], "ELIGIBLE")
        self.assertEqual(all_top[1638306]["competition_category"], "SENIOR_ELIGIBLE_CUP_OR_INTERNATIONAL")
        # Juveniles (U20, U21), reservas y divisiones inferiores verificadas excluidas
        self.assertEqual(all_top[1649151]["filter_status"], "EXCLUDED")
        self.assertEqual(all_top[1649151]["competition_category"], "YOUTH")
        self.assertEqual(all_top[1627125]["filter_status"], "EXCLUDED")
        self.assertEqual(all_top[1627125]["competition_category"], "YOUTH")
        self.assertEqual(all_top[1612183]["filter_status"], "EXCLUDED")
        self.assertEqual(all_top[1612183]["competition_category"], "RESERVES")
        self.assertEqual(all_top[1615817]["filter_status"], "EXCLUDED")
        self.assertEqual(all_top[1615817]["competition_category"], "LOWER_DIVISION")
        # Categorías desconocidas conservadas en REVIEW para auditoría, sin auto-promoción
        self.assertGreater(res_top["counts"]["review"], 0)
        self.assertTrue(all(r["competition_category"] == "UNKNOWN_CATEGORY" for r in res_top["review"]))

        # Punto 4: Copa de la Reina (1248) y WSL Cup (697) permanecen pendientes únicamente por falta de evidencia de categoría
        res_open = fml.filter_document(data, raw, policy="adult-open")
        all_open = {r["fixture_id"]: r for r in res_open["fixtures"] + res_open["review"] + res_open["excluded"]}
        for policy_map in (all_top, all_open):
            cup_rows = [r for r in policy_map.values() if r["league_id"] in (1248, 697)]
            self.assertEqual(len(cup_rows), 8)
            for cr in cup_rows:
                self.assertEqual(cr["filter_status"], "REVIEW")
                self.assertEqual(cr["filter_reason"], "TIER_UNKNOWN")
                self.assertEqual(cr["competition_category"], "UNKNOWN_CATEGORY")
                self.assertEqual(cr["matching_eligibility"], "PENDING_CLASSIFICATION")

        # C) Copa adulta con participante filial queda excluida por verificación de participantes
        cup_with_reserve = {
            "fixture_id": 999001, "country": "Spain", "league": "Copa del Rey", "league_id": 143,
            "season": 2026, "home": "Real Madrid", "away": "Real Madrid B", "status": "NS",
            "kickoff_madrid": "2026-11-18T21:00:00+01:00",
        }
        st_cup, rsn_cup = fml.classify(cup_with_reserve, policy="top-tier-and-senior-cups")
        self.assertEqual((st_cup, rsn_cup), ("EXCLUDED", "RESERVE_TEAM"))

    def test_02_family_minimum_coverage_and_no_extra_family_bonus(self) -> None:
        now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        # Partido A: solo goles (n=12), sin corners/tarjetas/tiros/xG -> MINIMUM_EVIDENCE_MET, compite en ranking
        h_hist = [{"id": 100 + i, "date_utc": (now - timedelta(days=i + 2)).isoformat(), "status": "FT", "home_id": 1, "away_id": 50 + i, "league_id": 71, "season": 2026, "goals_home": 2, "goals_away": 1, "team_goals": 2, "opponent_goals": 1, "venue": "HOME" if i % 2 == 0 else "AWAY"} for i in range(12)]
        a_hist = [{"id": 200 + i, "date_utc": (now - timedelta(days=i + 2)).isoformat(), "status": "FT", "home_id": 60 + i, "away_id": 2, "league_id": 71, "season": 2026, "goals_home": 1, "goals_away": 1, "team_goals": 1, "opponent_goals": 1, "venue": "AWAY" if i % 2 == 0 else "HOME"} for i in range(12)]
        # Partido B: datos insuficientes (n=2) -> BELOW_MINIMUM_EVIDENCE
        h_short = h_hist[:2]
        a_short = a_hist[:2]
        # Partido C: sin historial -> NO_STATISTICAL_DATA
        base_f = {"league_id": 71, "season": 2026, "country": "Brazil", "league": "Serie A", "gender": "M", "region": "SOUTH_AMERICA", "region_priority": 1, "kickoff_utc": (now + timedelta(hours=5)).isoformat(), "kickoff_madrid": (now + timedelta(hours=6)).isoformat()}

        m_goals_only = pm.evaluate_fixture(
            {**base_f, "fixture_id": 1001, "home": "Team A", "away": "Team B", "home_id": 1, "away_id": 2},
            {1: h_hist, 2: a_hist}, {}, {}, {}, now, detail_limit=8, details_selected=True,
        )
        m_short = pm.evaluate_fixture(
            {**base_f, "fixture_id": 1002, "home": "Team C", "away": "Team D", "home_id": 3, "away_id": 4},
            {3: h_short, 4: a_short}, {}, {}, {}, now, detail_limit=8, details_selected=True,
        )
        m_none = pm.evaluate_fixture(
            {**base_f, "fixture_id": 1003, "home": "Team E", "away": "Team F", "home_id": 5, "away_id": 6},
            {}, {}, {}, {}, now, detail_limit=8, details_selected=True,
        )
        self.assertEqual(m_goals_only["evidence_tier"], "MINIMUM_EVIDENCE_MET")
        self.assertIn("goals", m_goals_only["eligible_market_families"])
        self.assertTrue(m_goals_only["rank_eligible"])
        self.assertEqual(m_short["evidence_tier"], "BELOW_MINIMUM_EVIDENCE")
        self.assertFalse(m_short["rank_eligible"])
        self.assertEqual(m_none["evidence_tier"], "NO_STATISTICAL_DATA")
        self.assertFalse(m_none["rank_eligible"])

        # En rank_matches, goals-only compite en evaluated_research_ranking (si tiene research_priority_score)
        m_goals_only["research_priority_score"] = 65.0
        ranked = rm.rank_report(
            {"schema_version": "1.3-preselection", "generated_at_utc": now.isoformat(), "matches": [m_goals_only, m_short, m_none]},
            now=now,
        )
        self.assertEqual([x["fixture_id"] for x in ranked["evaluated_research_ranking"]], [1001])
        self.assertEqual({x["fixture_id"] for x in ranked["detailed_for_review"]}, {1002, 1003})

    def test_03_robustness_uncertainty_shrinkage_and_no_coverage_bonus(self) -> None:
        now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        # Muestra pequeña n=4 perfecta (4/4 = 100% sin varianza) vs muestra sólida n=14 consistente
        f_small = {"fixture_id": 2001, "home": "Small H", "away": "Small A", "home_id": 11, "away_id": 12, "league_id": 71, "season": 2026, "kickoff_madrid": (now + timedelta(hours=5)).isoformat(), "analysis_cutoff_utc": now.isoformat(), "statistical_priority": {"bonus": 7.0}}
        f_solid = {"fixture_id": 2002, "home": "Solid H", "away": "Solid A", "home_id": 21, "away_id": 22, "league_id": 71, "season": 2026, "kickoff_madrid": (now + timedelta(hours=5)).isoformat(), "analysis_cutoff_utc": now.isoformat(), "statistical_priority": {"bonus": 0.0}}

        res_small = tse.analyze_fixture(f_small, now, {"home": _make_obs(100, 11, 4, now, jitter=0.0), "away": _make_obs(200, 12, 4, now, jitter=0.0)})
        res_solid = tse.analyze_fixture(f_solid, now, {"home": _make_obs(300, 21, 14, now, jitter=0.1), "away": _make_obs(400, 22, 14, now, jitter=0.1)})

        self.assertGreater(res_solid["tactical_research_score"], res_small["tactical_research_score"])
        self.assertEqual(res_small["declared_coverage_bonus_applied"], 0.0)
        self.assertEqual(res_small["priority_score"], res_small["tactical_research_score"])
        sig_solid = res_solid["signals"][0]
        self.assertLess(sig_solid["uncertainty"]["laplace_shrunk_ceiling_rate"], 1.0)

    def test_04_budget_deferred_preserved_and_bwin_independent_statistical_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "2026-11-18_1500_0300"
            base = folder / "prospective_trial"
            now_m = datetime(2026, 11, 18, 16, 0, tzinfo=trial.TZ)
            ko = (now_m + timedelta(hours=4)).isoformat()
            cut = (now_m - timedelta(minutes=10)).isoformat()
            fx_list = [
                {"fixture_id": 3001, "home": "Alpha FC", "away": "Beta FC", "home_id": 1, "away_id": 2, "league_id": 71, "league": "Serie A", "country": "Brazil", "season": 2026, "kickoff_madrid": ko, "status": "NS", "gender": "M"},
                {"fixture_id": 3002, "home": "Gamma FC", "away": "Delta FC", "home_id": 3, "away_id": 4, "league_id": 71, "league": "Serie A", "country": "Brazil", "season": 2026, "kickoff_madrid": ko, "status": "NS", "gender": "M"},
            ]
            trial.dump(folder / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": fx_list})
            pre_matches = [
                {**fx_list[0], "analysis_cutoff_utc": cut, "details_checked": True, "data_readiness_score": 80, "research_priority_score": 80, "evidence_tier": "RICH_EVIDENCE_AVAILABLE", "statistical_observations": {"home": _make_obs(500, 1, 12, now_m.astimezone(timezone.utc)), "away": _make_obs(600, 2, 12, now_m.astimezone(timezone.utc))}},
                {**fx_list[1], "analysis_cutoff_utc": cut, "details_checked": True, "data_readiness_score": 80, "research_priority_score": 75, "evidence_tier": "MINIMUM_EVIDENCE_MET", "statistical_observations": {"home": _make_obs(700, 3, 10, now_m.astimezone(timezone.utc), corners=None), "away": _make_obs(800, 4, 10, now_m.astimezone(timezone.utc), corners=None)}},
            ]
            pre_path = base / "enriched/preselection.json"
            trial.dump(pre_path, {"schema_version": "1.3-preselection", "generated_at_utc": cut, "matches": pre_matches})

            # Ranking estadístico idéntico sin Bwin
            eng = tse.run_engine(pre_path, None, base / "tactical", ROOT, asof=now_m, use_cache=False)
            self.assertEqual({r["fixture_id"] for r in eng["statistical_ranking"]}, {3001, 3002})

            # Ledger con 3001 DIFERIDO_POR_PRESUPUESTO y 3002 NOT_FOUND_IN_SCANNED_PAGES
            trial.dump(base / "bwin/bwin_discovery.json", {"matches": [{"fixture_id": 3002, "status": "NOT_FOUND_IN_SCANNED_PAGES"}]})
            ledger = CandidateLedger(base / "candidate_ledger.json")
            ledger.record(fx_list[0], "DIFERIDO_POR_PRESUPUESTO", "capture", "MAX_SCRAPES_BUDGET_NOT_ANALYZED")
            ledger.record(fx_list[1], "SIN_BWIN", "discovery_validation", "NOT_FOUND_IN_SCANNED_PAGES")
            ledger.write()

            with patch.object(trial, "now_madrid", return_value=now_m):
                rows = {r["fixture_id"]: r for r in prematch_daily_csv.export_daily(base)}
            self.assertEqual(rows[3001]["estado_final"], "NO_EVALUADO_POR_LIMITE")
            self.assertEqual(rows[3001]["budget_status"], "PENDING_BUDGET")
            self.assertEqual(rows[3001]["bwin_search_status"], "BWIN_NOT_SCANNED_BUDGET")
            self.assertEqual(rows[3002]["bwin_search_status"], "BWIN_NOT_FOUND_IN_SCANNED_PAGES")
            self.assertEqual(rows[3002]["offer_proven_absent"], "false")

            denoms = json.loads((base / "pipeline_funnel_denominators.json").read_text(encoding="utf-8"))
            self.assertEqual(denoms["discovered_n"], 2)
            self.assertEqual(denoms["statistically_eligible_n"], 2)
            self.assertEqual(denoms["pending_budget_n"], 1)
            self.assertEqual(denoms["pending_bwin_n"], 2)

    def test_05_manual_bwin_url_cli_multi_duplicates_conflicts_and_scrape_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eval_dir = Path(td) / "outputs/discovery/2026-11-18_1500_0300/evaluations/eval1"
            base = eval_dir / "prospective_trial"
            now_m = trial.now_madrid()
            now_utc = now_m.astimezone(timezone.utc)
            ko_m = (now_m + timedelta(hours=5)).isoformat()
            fx_rows = [
                {"fixture_id": 1628569, "home": "Ranchers Bees", "away": "Doma United", "home_id": 101, "away_id": 102, "league": "NPFL", "league_id": 399, "season": 2026, "gender": "M", "kickoff_madrid": ko_m, "status": "NS", "filter_status": "ELIGIBLE", "competition_category": "SENIOR_MALE_TOP_TIER"},
                {"fixture_id": 1645880, "home": "Cotonsport", "away": "Stade Renard", "home_id": 103, "away_id": 104, "league": "Elite One", "league_id": 411, "season": 2026, "gender": "M", "kickoff_madrid": ko_m, "status": "NS", "filter_status": "ELIGIBLE", "competition_category": "SENIOR_MALE_TOP_TIER"},
                {"fixture_id": 999888, "home": "Real Madrid", "away": "Barcelona", "home_id": 541, "away_id": 529, "league": "La Liga", "league_id": 140, "season": 2026, "gender": "M", "kickoff_madrid": ko_m, "status": "FT", "filter_status": "EXCLUDED", "filter_reason": "NOT_PREMATCH"},
            ]
            trial.dump(eval_dir / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": fx_rows})
            trial.dump(eval_dir / "fixtures_major.json", {"schema_version": "1.0-major-leagues", "fixtures": fx_rows[:2], "review": [], "excluded": [fx_rows[2]]})
            pre_path = base / "enriched/preselection.json"
            trial.dump(pre_path, {"schema_version": "1.3-preselection", "generated_at_utc": now_utc.isoformat(), "matches": fx_rows[:2]})
            ledger = CandidateLedger(base / "candidate_ledger.json")
            for fx in fx_rows[:2]:
                ledger.record(fx, "SIN_BWIN", "discovery", "NOT_FOUND_IN_SCANNED_PAGES")
            ledger.write()

            # IDs sintéticos ficticios solo para prueba unitaria
            url1 = "https://www.bwin.es/es/sports/eventos/ranchers-bees-doma-united-2:7990001"
            url2 = "https://www.bwin.es/es/sports/eventos/cotonsport-stade-renard-2:7990002"

            # 1) CLI multi-URL offline -> BWIN_MANUAL_LINK_PENDING_VALIDATION
            res = subprocess.run(
                [
                    sys.executable, str(ROOT / "prematch_daily.py"),
                    "--root", str(td), "--eval-dir", str(eval_dir),
                    "--bind-bwin-url",
                    "--manual-bwin-url", f"1628569={url1}",
                    "--manual-bwin-url", f"1645880={url2}",
                    "--now-utc", now_utc.isoformat(),
                ],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            out_json = json.loads(res.stdout)
            self.assertEqual(out_json["registration"]["registered_count"], 2)
            store = bml.load_manual_links(eval_dir)
            self.assertEqual(store["links"]["1628569"]["extracted_event_id"], "7990001")
            self.assertEqual(store["links"]["1628569"]["identity_status"], "BWIN_MANUAL_LINK_PENDING_VALIDATION")
            self.assertEqual(store["links"]["1628569"]["quote_capture_status"], "NOT_CAPTURED")
            self.assertFalse(store["links"]["1628569"]["statistical_ranking_modified"])

            # 2) Rechazar URL sin Event ID, duplicado contradictorio, mismo event_id en otro fixture, o partido no PREMATCH
            with self.assertRaises(bml.ManualBwinLinkError):
                bml.register_manual_bwin_urls(eval_dir, [(1628569, "https://www.bwin.es/es/sports/futbol-4")], now_utc=now_utc)
            with self.assertRaises(bml.ManualBwinLinkError):
                bml.register_manual_bwin_urls(eval_dir, [(1628569, url1), (1628569, url2)], now_utc=now_utc)
            with self.assertRaises(bml.ManualBwinLinkError):
                bml.register_manual_bwin_urls(eval_dir, [(1645880, url1)], now_utc=now_utc)
            with self.assertRaises(bml.ManualBwinLinkError):
                bml.register_manual_bwin_urls(eval_dir, [(999888, "https://www.bwin.es/es/sports/eventos/real-madrid-barcelona-2:7990003")], now_utc=now_utc)

            # 3) Slug que coincide positivamente con OTRO fixture de la evaluación -> BWIN_IDENTITY_CONFLICT
            bad_slug_url = "https://www.bwin.es/es/sports/eventos/cotonsport-stade-renard-2:7990099"
            bad_reg = bml.register_manual_bwin_urls(eval_dir, [(1628569, bad_slug_url)], now_utc=now_utc)
            self.assertEqual(bad_reg["conflict_count"], 1)
            self.assertEqual(bad_reg["conflicts"][0]["identity_status"], "BWIN_IDENTITY_CONFLICT")

            # 4) Revalidar URL correcta (live mock) -> BWIN_MANUAL_LINK_VERIFIED sin alterar preselection
            pre_sha_before = trial.sha(pre_path)
            bml.register_manual_bwin_urls(eval_dir, [(1628569, url1)], now_utc=now_utc)

            class FakeBrowser:
                def close(self): pass
            class FakePlaywright:
                def __enter__(self):
                    return type("P", (), {"chromium": type("C", (), {"launch": staticmethod(lambda **kw: FakeBrowser())})()})()
                def __exit__(self, *a): pass

            mock_summary = {
                "fixture_id": 1628569, "home": "Ranchers Bees", "away": "Doma United",
                "bwin_event_id": "7990001", "bwin_url": url1,
                "status": "IDENTITY_VERIFIED_AUTOMATICALLY", "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                "category_corroboration_status": "CORROBORATED",
                "inspection": {
                    "status": "OPENED", "page_title": "Ranchers Bees - Doma United | NPFL",
                    "title_teams_match": True, "event_id_from_url": "7990001",
                    "event_start_machine_readable": ko_m, "kickoff_difference_minutes": 0.0,
                    "page_category_compatibility": "COMPATIBLE_OR_UNKNOWN", "competition_match_score": 1.0,
                    "supporting_identity_checks": {"competition_compatible": True, "category_compatible": True},
                    "market_headings": {},
                },
            }
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=mock_summary):
                ver = bml.verify_manual_bwin_links_online(eval_dir, [1628569])
            self.assertEqual(len(ver["verified"]), 1)
            self.assertEqual(ver["verified"][0]["identity_status"], "BWIN_MANUAL_LINK_VERIFIED")
            self.assertEqual(ver["verified"][0]["quote_capture_status"], "NOT_CAPTURED")
            self.assertEqual(trial.sha(pre_path), pre_sha_before)

    def test_06_manual_bwin_negative_checks_and_translated_names(self) -> None:
        class FakeBrowser:
            def close(self): pass
        class FakePlaywright:
            def __enter__(self):
                return type("P", (), {"chromium": type("C", (), {"launch": staticmethod(lambda **kw: FakeBrowser())})()})()
            def __exit__(self, *a): pass

        with tempfile.TemporaryDirectory() as td:
            eval_dir = Path(td) / "outputs/discovery/2026-11-18_1500_0300/evaluations/eval_neg"
            now_m = trial.now_madrid()
            now_utc = now_m.astimezone(timezone.utc)
            ko_m = (now_m + timedelta(hours=5)).isoformat()
            fx_rows = [
                {"fixture_id": 5001, "home": "Germany", "away": "Slovakia", "home_id": 25, "away_id": 773, "league": "UEFA Nations League", "league_id": 5, "season": 2026, "country": "World", "gender": "M", "kickoff_madrid": ko_m, "status": "NS", "filter_status": "ELIGIBLE"},
                {"fixture_id": 5002, "home": "Crvena Zvezda", "away": "Olympiacos", "home_id": 598, "away_id": 553, "league": "UEFA Champions League", "league_id": 2, "season": 2026, "country": "World", "gender": "M", "kickoff_madrid": ko_m, "status": "NS", "filter_status": "ELIGIBLE"},
            ]
            trial.dump(eval_dir / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": fx_rows})
            trial.dump(eval_dir / "fixtures_major.json", {"schema_version": "1.0-major-leagues", "fixtures": fx_rows, "review": [], "excluded": []})

            # Caso válido con nombres traducidos (no bloqueado offline ni por alias válido)
            url_es = "https://www.bwin.es/es/sports/eventos/alemania-eslovaquia-2:7980010"
            url_uncatalogued_es = "https://www.bwin.es/es/sports/eventos/estrella-roja-olimpiakos-2:7980011"
            reg_es = bml.register_manual_bwin_urls(eval_dir, [(5001, url_es), (5002, url_uncatalogued_es)], now_utc=now_utc)
            self.assertEqual(reg_es["conflict_count"], 0)
            self.assertEqual(reg_es["registered_count"], 2)

            base_obs = {
                "status": "OPENED",
                "page_title": "Alemania - Eslovaquia | Liga de Naciones de la UEFA",
                "title_teams_match": True,
                "event_id_from_url": "7980010",
                "event_start_machine_readable": ko_m,
                "kickoff_difference_minutes": 0.0,
                "page_category_compatibility": "COMPATIBLE_OR_UNKNOWN",
                "competition_match_score": 1.0,
                "supporting_identity_checks": {"competition_compatible": True, "category_compatible": True},
                "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
            }
            base_res = {
                "fixture_id": 5001, "home": "Germany", "away": "Slovakia",
                "bwin_event_id": "7980010", "bwin_url": url_es,
                "status": "IDENTITY_VERIFIED_AUTOMATICALLY",
                "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH", "category_corroboration_status": "CORROBORATED",
                "inspection": base_obs,
            }

            # 1) Caso válido con nombres traducidos en live -> BWIN_MANUAL_LINK_VERIFIED
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=copy.deepcopy(base_res)):
                ok_res = bml.verify_manual_bwin_links_online(eval_dir, [5001])
            self.assertEqual(len(ok_res["verified"]), 1)
            self.assertEqual(ok_res["verified"][0]["identity_status"], "BWIN_MANUAL_LINK_VERIFIED")

            # 2) Competición incompatible -> BWIN_IDENTITY_CONFLICT (comp_ok interviene en id_ok)
            bml.register_manual_bwin_urls(eval_dir, [(5001, url_es)], now_utc=now_utc)
            # Forzar re-registro pendiente sobrescribiendo para probar casos negativos
            store = bml.load_manual_links(eval_dir)
            def _reset_pending():
                store["links"]["5001"]["identity_status"] = "BWIN_MANUAL_LINK_PENDING_VALIDATION"
                store["links"]["5001"]["conflict_reasons"] = []
                bml._atomic_json(bml.manual_links_path(eval_dir), store)

            _reset_pending()
            bad_comp = copy.deepcopy(base_res)
            bad_comp["inspection"]["competition_match_score"] = 0.25
            bad_comp["inspection"]["supporting_identity_checks"]["competition_compatible"] = False
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=bad_comp):
                r = bml.verify_manual_bwin_links_online(eval_dir, [5001])
            self.assertEqual(len(r["failed"]), 1)
            self.assertEqual(r["failed"][0]["identity_status"], "BWIN_IDENTITY_CONFLICT")
            self.assertTrue(any("COMPETITION_INCOMPATIBLE" in x for x in r["failed"][0]["conflict_reasons"]))

            # 3a) Event ID observado ausente -> NO verifica ni cae en `or eid`; queda PENDING
            _reset_pending()
            no_eid = copy.deepcopy(base_res)
            no_eid["inspection"]["event_id_from_url"] = None
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=no_eid):
                r = bml.verify_manual_bwin_links_online(eval_dir, [5001])
            self.assertEqual(len(r["verified"]), 0)
            self.assertEqual(len(r["pending"]), 1)
            self.assertIn("OBSERVED_EVENT_ID_ABSENT", r["pending"][0]["pending_reasons"])

            # 3b) Event ID observado diferente -> BWIN_IDENTITY_CONFLICT
            _reset_pending()
            diff_eid = copy.deepcopy(base_res)
            diff_eid["inspection"]["event_id_from_url"] = "7999999"
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=diff_eid):
                r = bml.verify_manual_bwin_links_online(eval_dir, [5001])
            self.assertEqual(len(r["failed"]), 1)
            self.assertTrue(any("OBSERVED_EVENT_ID_MISMATCH" in x for x in r["failed"][0]["conflict_reasons"]))

            # 4a) Kickoff incorrecto -> BWIN_IDENTITY_CONFLICT
            _reset_pending()
            bad_ko = copy.deepcopy(base_res)
            bad_ko["status"] = "EVENT_REQUIRES_REVIEW"
            bad_ko["inspection"]["identity_status"] = "KICKOFF_MISMATCH_REVIEW"
            bad_ko["inspection"]["kickoff_difference_minutes"] = 120.0
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=bad_ko):
                r = bml.verify_manual_bwin_links_online(eval_dir, [5001])
            self.assertEqual(len(r["failed"]), 1)
            self.assertTrue(any("KICKOFF" in x for x in r["failed"][0]["conflict_reasons"]))

            # 4b) Kickoff no verificado (sin timestamp absoluto) -> PENDING (no verificado ni conflicto falso)
            _reset_pending()
            unver_ko = copy.deepcopy(base_res)
            unver_ko["status"] = "EVENT_FOUND_NEEDS_TIME_CONFIRMATION"
            unver_ko["inspection"]["identity_status"] = "NAME_MATCH_TIME_NOT_MACHINE_VERIFIED"
            unver_ko["inspection"]["event_start_machine_readable"] = None
            unver_ko["inspection"]["kickoff_difference_minutes"] = None
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=unver_ko):
                r = bml.verify_manual_bwin_links_online(eval_dir, [5001])
            self.assertEqual(len(r["verified"]), 0)
            self.assertEqual(len(r["pending"]), 1)
            self.assertIn("ABSOLUTE_KICKOFF_NOT_VERIFIED", r["pending"][0]["pending_reasons"])

            # 5a) Equipo visitante incorrecto -> BWIN_IDENTITY_CONFLICT
            _reset_pending()
            bad_away = copy.deepcopy(base_res)
            bad_away["status"] = "EVENT_REQUIRES_REVIEW"
            bad_away["inspection"]["page_title"] = "Alemania - Francia | UEFA Nations League"
            bad_away["inspection"]["title_teams_match"] = False
            bad_away["inspection"]["identity_status"] = "TITLE_MISMATCH_REVIEW"
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=bad_away):
                r = bml.verify_manual_bwin_links_online(eval_dir, [5001])
            self.assertEqual(len(r["failed"]), 1)
            self.assertIn("TITLE_TEAMS_MISMATCH", r["failed"][0]["conflict_reasons"])

            # 5b) Orden invertido de equipos -> BWIN_IDENTITY_CONFLICT
            _reset_pending()
            rev_order = copy.deepcopy(base_res)
            rev_order["inspection"]["page_title"] = "Eslovaquia - Alemania | UEFA Nations League"
            with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright()), \
                 patch("bwin_discovery.summarize", return_value=rev_order):
                r = bml.verify_manual_bwin_links_online(eval_dir, [5001])
            self.assertEqual(len(r["failed"]), 1)
            self.assertTrue(any("TEAM_ORDER_REVERSED_CONFLICT" in x for x in r["failed"][0]["conflict_reasons"]))

    def test_07_discovery_for_auto_vs_manual_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eval_dir = Path(td) / "outputs/discovery/2026-11-18_1500_0300/evaluations/eval_rec"
            base = eval_dir / "prospective_trial"
            now_m = trial.now_madrid()
            now_utc = now_m.astimezone(timezone.utc)
            ko_dt = now_m + timedelta(hours=5)
            ko_m = ko_dt.isoformat()
            day_label = "Hoy" if ko_dt.date() == now_m.date() else "Mañana"
            fx = {
                "fixture_id": 6001, "home": "Coritiba", "away": "Santos",
                "home_id": 147, "away_id": 128, "league": "Serie A", "league_id": 71,
                "season": 2026, "gender": "M", "kickoff_madrid": ko_m, "status": "NS",
                "filter_status": "ELIGIBLE", "matching_eligibility": "ELIGIBLE",
                "category_status": "ELIGIBLE", "category_evidence_status": "CORROBORATED", "category_level": "SENIOR",
            }
            trial.dump(eval_dir / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": [fx]})
            trial.dump(eval_dir / "fixtures_major.json", {"schema_version": "1.0-major-leagues", "fixtures": [fx], "review": [], "excluded": []})

            # Caso A: descubrimiento automático YA acreditado (7901452) + vinculación manual contradictoria (7999999)
            auto_url = "https://www.bwin.es/es/sports/eventos/coritiba-santos-2:7901452"
            manual_url = "https://www.bwin.es/es/sports/eventos/coritiba-santos-2:7999999"
            auto_disc = {
                "schema_version": "0.5-bwin-discovery-v3",
                "generated_at_utc": now_utc.isoformat(),
                "catalog_observed": [{
                    "bwin_event_id": "7901452", "bwin_url": auto_url,
                    "text": f"Coritiba Santos {day_label} / {ko_dt.strftime('%H:%M')}",
                    "text_observed_at_utc": now_utc.isoformat(),
                }],
                "matches": [{
                    **fx, "status": "IDENTITY_VERIFIED_AUTOMATICALLY", "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                    "bwin_event_id": "7901452", "bwin_url": auto_url, "name_match_score": 1.0,
                    "team_order_status": "HOME_AWAY", "category_compatibility": "COMPATIBLE_OR_UNKNOWN",
                    "inspection": {
                        "status": "OPENED", "page_title": "Coritiba - Santos | Serie A",
                        "title_teams_match": True, "event_id_from_url": "7901452",
                        "event_start_machine_readable": ko_m, "kickoff_difference_minutes": 0.0,
                        "time_evidence_type": "ABSOLUTE_JSON_LD",
                        "observed_at_utc": now_utc.isoformat(),
                    },
                }],
            }
            trial.dump(base / "bwin/shared/bwin_discovery.json", auto_disc)

            # Registrar URL manual contradictoria: debe quedar marcada BWIN_IDENTITY_CONFLICT
            reg_conf = bml.register_manual_bwin_urls(eval_dir, [(6001, manual_url)], now_utc=now_utc)
            self.assertEqual(reg_conf["conflict_count"], 1)
            self.assertEqual(reg_conf["conflicts"][0]["auto_reconciliation"]["status"], "CONFLICT_WITH_ACCREDITED_AUTO")

            # Y en prematch_runtime.discovery_for(): no ignora silenciosamente la discrepancia y preserva el auto acreditado
            failures: list[dict] = []
            args = type("Args", (), {"min_lead": 45, "confirm_external": False, "bwin_identity_catalog": None, "discovery_retries": 0})()
            chosen_path, chosen_match = prematch_runtime.discovery_for(Path(td), base, fx, args, failures)
            self.assertEqual(chosen_match["bwin_event_id"], "7901452")
            self.assertTrue(any(f.get("phase") == "discovery_manual_reconciliation" and f.get("reason") == "AUTO_VS_MANUAL_EVENT_ID_CONFLICT" for f in failures))

            # Caso B: descubrimiento automático NO acreditado (sin hora verificada ni en página ni en catálogo) + manual verificado distinto -> registra discrepancia y usa manual verificado
            auto_disc["catalog_observed"][0]["text"] = "Coritiba Santos"
            auto_disc["matches"][0]["status"] = "EVENT_FOUND_NEEDS_TIME_CONFIRMATION"
            auto_disc["matches"][0]["identity_status"] = "NAME_MATCH_TIME_NOT_MACHINE_VERIFIED"
            auto_disc["matches"][0]["inspection"]["event_start_machine_readable"] = None
            auto_disc["matches"][0]["inspection"]["kickoff_difference_minutes"] = None
            auto_disc["matches"][0]["inspection"]["time_evidence_type"] = "NONE"
            trial.dump(base / "bwin/shared/bwin_discovery.json", auto_disc)

            manual_disc_path = base / "bwin/manual_6001_verified/bwin_discovery.json"
            trial.dump(manual_disc_path, {
                "schema_version": "0.5-bwin-discovery-v3",
                "generated_at_utc": now_utc.isoformat(),
                "catalog_observed": [{
                    "bwin_event_id": "7999999", "bwin_url": manual_url,
                    "text": "coritiba santos 2 7999999", "text_observed_at_utc": now_utc.isoformat(),
                }],
                "matches": [{
                    **fx, "status": "IDENTITY_VERIFIED_AUTOMATICALLY", "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                    "bwin_event_id": "7999999", "bwin_url": manual_url, "name_match_score": 1.0,
                    "team_order_status": "HOME_AWAY", "category_compatibility": "COMPATIBLE_OR_UNKNOWN",
                    "category_corroboration_status": "CORROBORATED",
                    "inspection": {
                        "status": "OPENED", "page_title": "Coritiba - Santos | Serie A",
                        "title_teams_match": True, "event_id_from_url": "7999999",
                        "event_start_machine_readable": ko_m, "kickoff_difference_minutes": 0.0,
                        "time_evidence_type": "ABSOLUTE_JSON_LD",
                        "competition_match_score": 1.0,
                        "page_category_compatibility": "COMPATIBLE_OR_UNKNOWN",
                        "supporting_identity_checks": {"competition_compatible": True, "category_compatible": True},
                        "observed_at_utc": now_utc.isoformat(),
                    },
                }],
            })
            bml._atomic_json(bml.manual_links_path(eval_dir), {
                "schema_version": bml.MANUAL_LINKS_SCHEMA,
                "links": {
                    "6001": {
                        "fixture_id": 6001,
                        "extracted_event_id": "7999999",
                        "bwin_url": manual_url,
                        "identity_status": "BWIN_MANUAL_LINK_VERIFIED",
                        "verification": {"discovery_json": str(manual_disc_path)},
                    }
                },
            })
            failures_b: list[dict] = []
            chosen_path_b, chosen_match_b = prematch_runtime.discovery_for(Path(td), base, fx, args, failures_b)
            self.assertEqual(chosen_match_b["bwin_event_id"], "7999999")
            self.assertTrue(any(f.get("phase") == "discovery_manual_reconciliation" and f.get("reason") == "DISCREPANCY_MANUAL_VS_UNVERIFIED_AUTO" for f in failures_b))
            rec_store = bml.load_manual_links(eval_dir)["links"]["6001"]
            self.assertEqual(rec_store["auto_reconciliation"]["status"], "DISCREPANCY_WITH_UNVERIFIED_AUTO")


if __name__ == "__main__":
    unittest.main()
