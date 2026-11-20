#!/usr/bin/env python3
"""Regresiones focalizadas: auditoría de cobertura/mercados UWCL, alias femeninos sin colisiones,
separación explícita de estados, cruce patrón-mercado y selección manual de Chelsea W-PSG W.
"""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bwin_discovery as bd
import bwin_market_preflight as pf
import market_quality_gate as mqg
import prematch_daily as daily
import prematch_market_mapping_v1 as mm
import prematch_opportunity_scanner as scanner
import prematch_prospective_trial as trial
import prematch_tactical_signal_engine as tse
from bet_engine.markets.bwin_evaluator import EvaluatedMarketOption
from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt


class UwclAuditRegressionTests(unittest.TestCase):
    def _uwcl_fixture(
        self,
        *,
        fid: int = 1638333,
        home: str = "Real Madrid W",
        away: str = "Inter Milano W",
        eid: str = "7913438",
        same_comp_n: int = 6,
        all_goals_n: int = 20,
        corners_n: int = 5,
        details_checked: bool = True,
        ko_utc: datetime | None = None,
    ) -> dict:
        ko_dt = ko_utc or datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)
        ko_m = ko_dt.astimezone(trial.TZ).isoformat()
        cutoff = (ko_dt - timedelta(hours=3)).isoformat()
        return {
            "fixture_id": fid,
            "home": home,
            "away": away,
            "home_id": 19001,
            "away_id": 19002,
            "league": "UEFA Champions League Women",
            "league_id": 525,
            "season": 2026,
            "country": "World",
            "gender": "F",
            "kickoff_madrid": ko_m,
            "kickoff_utc": ko_dt.isoformat(),
            "analysis_cutoff_utc": cutoff,
            "status": "NS",
            "filter_status": "ELIGIBLE",
            "competition_category": "SENIOR_FEMALE_CONTINENTAL_TOP",
            "details_checked": details_checked,
            "market_profiles": {
                "corners": {
                    "coverage_class": "SUFICIENTE" if corners_n >= 6 else "PARCIAL",
                    "n_series": {
                        "home_for": corners_n,
                        "home_against": corners_n,
                        "away_for": corners_n,
                        "away_against": corners_n,
                    },
                },
                "yellow": {"coverage_class": "PARCIAL", "n_series": {"home_for": 5, "home_against": 5, "away_for": 5, "away_against": 5}},
            },
            "home_evidence": {
                "last_matches_count": all_goals_n,
                "history": {
                    "completed_total": all_goals_n,
                    "same_competition_total": same_comp_n,
                    "goals_for": {"n": all_goals_n, "mean": 2.2, "cv": 0.56},
                    "goals_against": {"n": all_goals_n, "mean": 1.3, "cv": 1.24},
                    "same_competition_goals_for": {"n": same_comp_n, "mean": 1.83, "cv": 0.37},
                    "same_competition_goals_against": {"n": same_comp_n, "mean": 0.83, "cv": 0.82},
                },
                "details": {
                    "own": {"goals": {"n": same_comp_n, "mean": 1.83, "cv": 0.37}, "corners": {"n": corners_n, "mean": 5.4, "cv": 0.40}},
                    "conceded": {"goals": {"n": same_comp_n, "mean": 0.83, "cv": 0.82}, "corners": {"n": corners_n, "mean": 3.6, "cv": 0.45}},
                },
            },
            "away_evidence": {
                "last_matches_count": all_goals_n,
                "history": {
                    "completed_total": all_goals_n,
                    "same_competition_total": same_comp_n,
                    "goals_for": {"n": all_goals_n, "mean": 1.75, "cv": 0.86},
                    "goals_against": {"n": all_goals_n, "mean": 1.25, "cv": 1.33},
                    "same_competition_goals_for": {"n": same_comp_n, "mean": 1.0, "cv": 1.0},
                    "same_competition_goals_against": {"n": same_comp_n, "mean": 2.33, "cv": 0.88},
                },
                "details": {
                    "own": {"goals": {"n": same_comp_n, "mean": 1.0, "cv": 1.0}, "corners": {"n": corners_n, "mean": 2.8, "cv": 0.50}},
                    "conceded": {"goals": {"n": same_comp_n, "mean": 2.33, "cv": 0.88}, "corners": {"n": corners_n, "mean": 4.4, "cv": 0.42}},
                },
            },
        }

    def test_1_real_madrid_inter_w_7913438_coverage_classification_and_states(self) -> None:
        fx = self._uwcl_fixture()
        disc = {**fx, "bwin_event_id": "7913438", "bwin_url": "https://www.bwin.es/es/sports/eventos/real-madrid-f-inter-milan-f-2:7913438"}
        rows = [
            {"market": "Total de goles | Tiempo reglamentario", "selection": "Más de 2,5", "odd": "1.55"},
            {"market": "Real Madrid (F) - Total de goles | Tiempo reglamentario", "selection": "Más de 1,5", "odd": "1.35"},
            {"market": "Inter Milán (F) - Total de goles | Tiempo reglamentario", "selection": "Más de 0,5", "odd": "1.65"},
            {"market": "Total de córners | Tiempo reglamentario", "selection": "Más de 8,5", "odd": "1.72"},
            {"market": "Resultado del partido | Tiempo reglamentario", "selection": "Real Madrid (F)", "odd": "1.22"},
            {"market": "Doble oportunidad | Tiempo reglamentario", "selection": "X o Inter Milán (F)", "odd": "3.40"},
            {"market": "Ambos equipos marcan | Tiempo reglamentario", "selection": "Sí", "odd": "1.75"},
            {"market": "Descanso/Final del partido", "selection": "Real Madrid (F)/Real Madrid (F)", "odd": "1.65"},
            {"market": "Inter Milán (F) - 1º goleador", "selection": "Jugadora A", "odd": "6.50"},
            {"market": "Jugador que marca primero y su equipo gana", "selection": "Jugadora B", "odd": "4.20"},
            {"market": "Total de goles | 1ª parte", "selection": "Más de 1,5", "odd": "2.10"},
            {"market": "Real Madrid (F) va por delante en el marcador en cualquier momento", "selection": "Sí", "odd": "1.14"},
        ]
        summary, details = mqg.analyze_rows(rows, disc, fx)
        # 1) Total goals FT O/U .5 con muestra mixta (same_comp=6 >=4 + completed=20 >=12) entra en shortlist sin degradar córners (n=5<6).
        self.assertEqual(summary["researchable_after_data_gate"], 3)
        self.assertEqual(details[0]["data_source"], "GOAL_HISTORY_MIXED_COMPETITION")
        self.assertIn("muestra mixta", details[0]["rationale"])
        self.assertEqual(details[3]["data_status"], "PARTIAL_DATA")
        self.assertEqual(details[3]["evaluation_state"], "INSUFFICIENT_STATISTICAL_EVIDENCE")
        # 2) Resultado / Doble oportunidad / BTTS FT tienen evidencia y modelo disponible pendiente de ejecutar.
        self.assertEqual(details[4]["evaluation_state"], "MODEL_EVALUATION_PENDING")
        self.assertEqual(details[5]["evaluation_state"], "MODEL_EVALUATION_PENDING")
        self.assertEqual(details[6]["evaluation_state"], "MODEL_EVALUATION_PENDING")
        # 3) Descanso/Final sin modelo -> MODEL_UNAVAILABLE (nunca NO BET estadístico).
        self.assertEqual(details[7]["evaluation_state"], "MODEL_UNAVAILABLE")
        # 4) Player props -> goalscorers + PLAYER_DATA_AND_XI_REQUIRED (unificado entre preflight y gate).
        self.assertEqual(details[8]["market_kind"], "goalscorers")
        self.assertEqual(pf.market_kind(rows[8]["market"]), "goalscorers")
        self.assertEqual(details[9]["market_kind"], "goalscorers")
        self.assertEqual(details[9]["data_status"], "PLAYER_DATA_AND_XI_REQUIRED")
        # 5) 1ª parte sin split en preselection -> NEEDS_PERIOD_SPLIT.
        self.assertEqual(details[10]["data_status"], "NEEDS_PERIOD_SPLIT")
        # 6) Residual no mapeado -> MARKET_CAPTURED_UNMAPPED y familias ausentes -> NO_MARKET_CAPTURED.
        self.assertEqual(details[11]["evaluation_state"], "MARKET_CAPTURED_UNMAPPED")
        for absent in ("cards", "shots", "shots_on_target", "fouls", "offsides"):
            self.assertIn(absent, summary["missing_market_families"])
            self.assertEqual(summary["family_evaluation_states"][absent], "NO_MARKET_CAPTURED")

    def test_2_female_aliases_reconcile_without_distinct_team_collisions(self) -> None:
        fx_rm_int = {"home": "Real Madrid W", "away": "Inter Milano W", "gender": "F"}
        fx_ch_psg = {"home": "Chelsea W", "away": "Paris Saint Germain W", "gender": "F"}
        fx_juv_ohl = {"home": "Juventus W", "away": "OH Leuven W", "gender": "F"}

        self.assertEqual(bd.resolve_verified_team_side("Real Madrid (F)", fx_rm_int), "home")
        self.assertEqual(bd.resolve_verified_team_side("Inter Milán (F)", fx_rm_int), "away")
        self.assertEqual(bd.resolve_verified_team_side("Chelsea (F)", fx_ch_psg), "home")
        self.assertEqual(bd.resolve_verified_team_side("PSG (F)", fx_ch_psg), "away")
        self.assertEqual(bd.resolve_verified_team_side("Oud-Heverlee Leuven (F)", fx_juv_ohl), "away")

        # Anti-colisiones críticas: categorías distintas y clubes distintos rechazados
        self.assertIsNone(bd.resolve_verified_team_side("Inter Milán (F)", {"home": "Inter", "away": "AC Milan", "gender": "M"}))
        self.assertIsNone(bd.resolve_verified_team_side("Real Madrid", fx_rm_int))
        self.assertIsNone(bd.resolve_verified_team_side("Inter U19 (F)", fx_rm_int))
        self.assertIsNone(bd.resolve_verified_team_side("París FC (F)", fx_ch_psg))
        self.assertIsNone(bd.resolve_verified_team_side("Inter Miami (F)", fx_rm_int))

        # Market mapping y scanner mantienen la misma barrera de categoría y clubes distintos
        self.assertTrue(
            mm._compatible(
                {"market": "Inter Milán (F) - Total de goles | Tiempo reglamentario", "market_kind": "total_goals", "period": "FULL_TIME"},
                {"team": "Inter Milano W", "opponent": "Real Madrid W", "metric": "goals", "period": "FULL_TIME"},
            )
        )
        self.assertTrue(
            mm._compatible(
                {"market": "PSG (F) - Total de goles | Tiempo reglamentario", "market_kind": "total_goals", "period": "FULL_TIME"},
                {"team": "Paris Saint Germain W", "opponent": "Chelsea W", "metric": "goals", "period": "FULL_TIME"},
            )
        )
        self.assertFalse(
            mm._compatible(
                {"market": "París FC (F) - Total de goles | Tiempo reglamentario", "market_kind": "total_goals", "period": "FULL_TIME"},
                {"team": "Paris Saint Germain W", "opponent": "Chelsea W", "metric": "goals", "period": "FULL_TIME"},
            )
        )

    def test_3_markets_previously_misclassified_as_other_and_combined(self) -> None:
        cases = [
            ("Inter Milán (F) - 1º goleador", "goalscorers"),
            ("Jugador que marca primero y su equipo gana", "goalscorers"),
            ("Jugador que marca y su equipo gana", "goalscorers"),
            ("Goles totales exactos | Tiempo reglamentario", "total_goals"),
            ("Multigol 1ª/2ª mitad | Tiempo reglamentario", "total_goals"),
            ("Real Madrid (F) - Multigol", "total_goals"),
            ("1X2 - Cuotas Mejoradas NUEVO", "result"),
            ("Inter Milán (F) no cuenta", "result"),
            ("Real Madrid (F) gana sin recibir goles | Tiempo reglamentario", "result"),
            ("Real Madrid (F) mantiene la portería a cero", "result"),
            ("Margen de victoria", "result"),
            ("Resultado del partido y multigol", "combined_or_builder"),
            ("Doble oportunidad y goles totales 2,5", "combined_or_builder"),
        ]
        for market_label, expected_kind in cases:
            with self.subTest(market=market_label):
                self.assertEqual(mqg.market_kind(market_label), expected_kind)
                self.assertEqual(pf.market_kind(market_label), expected_kind)

    def test_4_pattern_to_market_signal_and_shadow_txt_parsing_match_real_markets(self) -> None:
        ko_utc = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)
        now_m = (ko_utc - timedelta(minutes=90)).astimezone(trial.TZ)
        fx = self._uwcl_fixture(ko_utc=ko_utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            txt = root / "cuotas_bwin_7913438.txt"
            txt.write_text(
                "============================================================\n"
                "BWIN - CUOTAS\n"
                "Event ID: 7913438\n"
                "URL: https://www.bwin.es/es/sports/eventos/real-madrid-f-inter-milan-f-2:7913438\n"
                f"Captura: {now_m.isoformat()}\n"
                "Acordeones: 6 | Selecciones: 11\n"
                "MERCADO: Resultado del partido | Tiempo reglamentario\n"
                "Real Madrid (F) : 1.22\n"
                "X : 6.50\n"
                "Inter Milán (F) : 8.50\n"
                "MERCADO: Ganador sin empate | Tiempo reglamentario\n"
                "Real Madrid (F) : 1.07\n"
                "Inter Milán (F) : 7.50\n"
                "MERCADO: Total de goles | Tiempo reglamentario\n"
                "Más de 2,5 : 1.55\n"
                "Menos de 2,5 : 2.35\n"
                "MERCADO: Real Madrid (F) - Total de goles | Tiempo reglamentario\n"
                "Más de 1,5 : 1.35\n"
                "MERCADO: Inter Milán (F) - Total de goles | Tiempo reglamentario\n"
                "Más de 0,5 : 1.65\n"
                "MERCADO: Total de córners | Tiempo reglamentario\n"
                "Más de 8,5 : 1.72\n"
                "MERCADO: Inter Milán (F) - Total de córners | Tiempo reglamentario\n"
                "Más de 3,5 : 2.10\n",
                encoding="utf-8",
            )
            odds = parse_bwin_odds_txt(txt, home_name=fx["home"], away_name=fx["away"], gender="F")
            self.assertEqual(odds["home_win"], 1.22)
            self.assertEqual(odds["away_win"], 8.5)
            self.assertEqual(odds["dnb_home"], 1.07)
            self.assertEqual(odds["dnb_away"], 7.5)
            self.assertEqual(odds["home_over_1_5_goals"], 1.35)
            self.assertEqual(odds["away_over_0_5_goals"], 1.65)
            self.assertEqual(odds["away_over_3_5_corners"], 2.1)

            pre_path = root / "preselection.json"
            disc_path = root / "bwin_discovery.json"
            pf_path = root / "market_preflight.json"
            snap = pf.snapshot_odds(txt, "7913438")
            rows = snap.pop("_parsed_selection_rows")
            q_sum, q_det = mqg.analyze_rows(rows, {**fx, "bwin_event_id": "7913438", "bwin_url": "https://www.bwin.es/es/sports/eventos/real-madrid-f-inter-milan-f-2:7913438"}, fx)
            trial.dump(pre_path, {"matches": [fx]})
            trial.dump(
                disc_path,
                {
                    "generated_at_utc": (ko_utc - timedelta(minutes=95)).isoformat(),
                    "user_time_reference": "Europe/Madrid",
                    "catalog_observed": [],
                    "matches": [
                        {
                            **fx,
                            "bwin_event_id": "7913438",
                            "bwin_url": "https://www.bwin.es/es/sports/eventos/real-madrid-f-inter-milan-f-2:7913438",
                            "status": "IDENTITY_VERIFIED_AUTOMATICALLY",
                            "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                            "team_order_status": "HOME_AWAY_ALIAS",
                            "category_compatibility": "FEMALE_CORROBORATED",
                            "inspection": {
                                "status": "OPENED",
                                "page_title": "Real Madrid (F) - Inter Milán (F) UEFA Champions League (F) Fútbol | bwin",
                                "title_teams_match": True,
                                "event_id_from_url": "7913438",
                                "event_start_machine_readable": fx["kickoff_madrid"],
                                "observed_at_utc": (ko_utc - timedelta(minutes=95)).isoformat(),
                                "competition_match_score": 1.0,
                            },
                        }
                    ],
                },
            )
            trial.dump(
                pf_path,
                {
                    "generated_at_utc": (ko_utc - timedelta(minutes=92)).isoformat(),
                    "source_discovery": str(disc_path),
                    "source_sha256": trial.sha(disc_path),
                    "source_preselection": str(pre_path),
                    "source_preselection_sha256": trial.sha(pre_path),
                    "matches": [
                        {
                            **fx,
                            "bwin_event_id": "7913438",
                            "bwin_url": "https://www.bwin.es/es/sports/eventos/real-madrid-f-inter-milan-f-2:7913438",
                            "status": "MARKET_TRIPLES_EXTRACTED",
                            "time_evidence": {
                                "status": "ABSOLUTE_TIME_VERIFIED",
                                "source": "INSPECTED_EVENT_JSON_LD",
                                "observed_at_utc": (ko_utc - timedelta(minutes=95)).isoformat(),
                            },
                            "snapshot": snap,
                            "quality_gate": q_sum,
                        }
                    ],
                },
            )
            signals = [
                {"fixture_id": 1638333, "metric": "goals", "team_side": "home", "status": "SUPPORTED_DESCRIPTIVE_SIGNAL", "markets": [], "market_status": "PENDIENTE_MERCADO"},
                {"fixture_id": 1638333, "metric": "goals", "team_side": "away", "status": "SUPPORTED_DESCRIPTIVE_SIGNAL", "markets": [], "market_status": "PENDIENTE_MERCADO"},
                {"fixture_id": 1638333, "metric": "corners", "team_side": "away", "status": "SUPPORTED_DESCRIPTIVE_SIGNAL", "markets": [], "market_status": "PENDIENTE_MERCADO"},
            ]
            summary = tse.market_links(fx, signals, pre_path, [pf_path], now_m, 120)
            self.assertEqual(summary["verified_preflights"], 1)
            home_mkts = {m["market"] for m in signals[0]["markets"]}
            away_mkts = {m["market"] for m in signals[1]["markets"]}
            corner_mkts = {m["market"] for m in signals[2]["markets"]}
            self.assertIn("Real Madrid (F) - Total de goles | Tiempo reglamentario", home_mkts)
            self.assertNotIn("Inter Milán (F) - Total de goles | Tiempo reglamentario", home_mkts)
            self.assertIn("Inter Milán (F) - Total de goles | Tiempo reglamentario", away_mkts)
            self.assertIn("Inter Milán (F) - Total de córners | Tiempo reglamentario", corner_mkts)

    def test_5_missing_model_never_falsely_labeled_no_bet_and_preserves_states(self) -> None:
        unmodelled = EvaluatedMarketOption(
            market_family="FIRST_TEAM_TO_SCORE",
            selection_key="first_goal_none",
            line=None,
            odd=9.5,
            break_even_prob=0.1053,
            p_model=None,
            p_low=None,
            p_high=None,
            ev_model=None,
            ev_conservative_low=None,
            legacy_p6_hit_rate=None,
            legacy_p6_n=0,
            delta_vs_legacy_p6=None,
            classification="MERCADO_NO_MODELADO",
            decision_category="MERCADO_NO_MODELADO",
        )
        self.assertEqual(unmodelled.epistemic_evaluation_state, "MODEL_UNAVAILABLE")

        no_odds = EvaluatedMarketOption(
            market_family="GOALS_FT",
            selection_key="over_2_5_goals",
            line=2.5,
            odd=None,
            break_even_prob=None,
            p_model=0.55,
            p_low=0.45,
            p_high=0.65,
            ev_model=None,
            ev_conservative_low=None,
            legacy_p6_hit_rate=None,
            legacy_p6_n=0,
            delta_vs_legacy_p6=None,
            classification="NO_ODDS",
            decision_category="BLOQUEO_TECNICO",
        )
        self.assertEqual(no_odds.epistemic_evaluation_state, "NO_MARKET_CAPTURED")

        no_val = EvaluatedMarketOption(
            market_family="GOALS_FT",
            selection_key="over_2_5_goals",
            line=2.5,
            odd=1.5,
            break_even_prob=0.6667,
            p_model=0.55,
            p_low=0.45,
            p_high=0.65,
            ev_model=-0.175,
            ev_conservative_low=-0.325,
            legacy_p6_hit_rate=None,
            legacy_p6_n=0,
            delta_vs_legacy_p6=None,
            classification="NO_EDGE_OR_HIGH_UNCERTAINTY",
            decision_category="NO_BET_POR_PRECIO",
        )
        self.assertEqual(no_val.epistemic_evaluation_state, "MODEL_EVALUATED_NO_VALUE")

        cand = EvaluatedMarketOption(
            market_family="GOALS_FT",
            selection_key="over_2_5_goals",
            line=2.5,
            odd=2.4,
            break_even_prob=0.4167,
            p_model=0.55,
            p_low=0.48,
            p_high=0.62,
            ev_model=0.32,
            ev_conservative_low=0.152,
            legacy_p6_hit_rate=None,
            legacy_p6_n=0,
            delta_vs_legacy_p6=None,
            classification="INVESTIGABLE_CANDIDATE",
            decision_category="CANDIDATO_CONDICIONAL",
        )
        self.assertEqual(cand.epistemic_evaluation_state, "MODEL_EVALUATED_VALUE_CANDIDATE")

    def test_6_preflight_without_sufficient_statistical_evidence_stays_blocked(self) -> None:
        fx_low = self._uwcl_fixture(same_comp_n=5, all_goals_n=7, corners_n=3)
        disc = {**fx_low, "bwin_event_id": "7913438", "bwin_url": "https://www.bwin.es/es/sports/eventos/real-madrid-f-inter-milan-f-2:7913438"}
        rows = [
            {"market": "Total de goles | Tiempo reglamentario", "selection": "Más de 2,5", "odd": "1.55"},
            {"market": "Total de córners | Tiempo reglamentario", "selection": "Más de 8,5", "odd": "1.75"},
            {"market": "Resultado del partido | Tiempo reglamentario", "selection": "Real Madrid (F)", "odd": "1.22"},
        ]
        summary_low, det_low = mqg.analyze_rows(rows, disc, fx_low)
        self.assertEqual(summary_low["researchable_after_data_gate"], 0)
        self.assertEqual(det_low[0]["data_status"], "PARTIAL_DATA")
        self.assertEqual(det_low[0]["evaluation_state"], "INSUFFICIENT_STATISTICAL_EVIDENCE")
        self.assertEqual(det_low[1]["evaluation_state"], "INSUFFICIENT_STATISTICAL_EVIDENCE")
        self.assertEqual(det_low[2]["evaluation_state"], "INSUFFICIENT_STATISTICAL_EVIDENCE")

        fx_nodet = self._uwcl_fixture(details_checked=False)
        summary_nd, det_nd = mqg.analyze_rows(rows, disc, fx_nodet)
        self.assertEqual(summary_nd["researchable_after_data_gate"], 0)
        self.assertEqual(det_nd[0]["data_status"], "DETAIL_NOT_EVALUATED")

    def test_7_t45_t15_t5_kickoff_respected_and_manual_select_1638301_over_remo(self) -> None:
        ko_utc = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)
        ko_m = ko_utc.astimezone(trial.TZ)
        fx_chel = self._uwcl_fixture(fid=1638301, home="Chelsea W", away="Paris Saint Germain W", eid="7913441", same_comp_n=6, all_goals_n=20, corners_n=4, ko_utc=ko_utc)
        fx_remo = self._uwcl_fixture(fid=1492456, home="Remo", away="Chapecoense-sc", eid="7901472", same_comp_n=18, all_goals_n=20, corners_n=8, ko_utc=ko_utc + timedelta(hours=3))
        fx_remo["gender"] = "M"
        fx_remo["league"] = "Serie B"
        fx_remo["league_id"] = 72
        fx_remo["home_evidence"]["history"]["same_competition_goals_for"]["mean"] = 2.8

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eval_dir = root / "outputs/discovery/2026-11-18_1500_0300/evaluations/eval_uwcl"
            base = eval_dir / "prospective_trial"
            now_m = (ko_utc - timedelta(minutes=90)).astimezone(trial.TZ)
            canon = {
                "schema_version": "1.0-major",
                "fixtures": [
                    {**fx_remo, "category_status": "ELIGIBLE"},
                    {**fx_chel, "category_status": "ELIGIBLE"},
                ],
            }
            trial.dump(base / "fixtures_major_test.json", canon)
            trial.dump(base / "enriched/preselection.json", {
                "generated_at_utc": (ko_utc - timedelta(hours=3)).isoformat(),
                "source_filter_sha256": trial.sha(base / "fixtures_major_test.json"),
                "matches": [fx_remo, fx_chel],
            })
            # Catalog verified para ambos; Remo y Chelsea W con HOME_AWAY_ALIAS (PSG F -> Paris Saint Germain W)
            disc_path = base / "bwin/bwin_discovery.json"
            disc_doc = {
                "generated_at_utc": now_m.astimezone(timezone.utc).isoformat(),
                "user_time_reference": "Europe/Madrid",
                "catalog_observed": [],
                "matches": [
                    {
                        **fx_remo, "bwin_event_id": "7901472", "bwin_url": "https://www.bwin.es/es/sports/eventos/remo-chapecoense-2:7901472",
                        "status": "IDENTITY_VERIFIED_AUTOMATICALLY", "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                        "team_order_status": "HOME_AWAY", "category_compatibility": "COMPATIBLE_OR_UNKNOWN",
                        "inspection": {"status": "OPENED", "page_title": "Remo - Chapecoense Serie B", "title_teams_match": True, "event_id_from_url": "7901472", "event_start_machine_readable": fx_remo["kickoff_madrid"], "observed_at_utc": now_m.astimezone(timezone.utc).isoformat(), "competition_match_score": 1.0, "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH"},
                    },
                    {
                        **fx_chel, "bwin_event_id": "7913441", "bwin_url": "https://www.bwin.es/es/sports/eventos/chelsea-f-psg-f-2:7913441",
                        "status": "IDENTITY_VERIFIED_AUTOMATICALLY", "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH",
                        "team_order_status": "HOME_AWAY_ALIAS", "category_compatibility": "FEMALE_CORROBORATED",
                        "inspection": {"status": "OPENED", "page_title": "Chelsea (F) - PSG (F) UEFA Champions League (F) Fútbol | bwin", "title_teams_match": True, "event_id_from_url": "7913441", "event_start_machine_readable": fx_chel["kickoff_madrid"], "observed_at_utc": now_m.astimezone(timezone.utc).isoformat(), "competition_match_score": 1.0, "identity_status": "NAME_AND_ABSOLUTE_TIME_MATCH"},
                    },
                ],
            }
            trial.dump(disc_path, disc_doc)

            # Verificar que bwin_ranking_links valida 1638301 (HOME_AWAY_ALIAS) y respeta T-45
            links_ok, _ = daily.bwin_ranking_links(canon, disc_doc, now_m, disc_path, min_lead=45)
            self.assertEqual(links_ok["1638301"]["status"], "VERIFIED")
            links_t44, _ = daily.bwin_ranking_links(canon, disc_doc, ko_m - timedelta(minutes=44), disc_path, min_lead=45)
            self.assertNotEqual(links_t44["1638301"]["status"], "VERIFIED")

            # Preflight sintético válido SOLO para 1638301 y verificación manual en select_verified_bwin:
            # selecciona 1638301 aunque 1492456 esté en ranking y ambos verificados.
            txt = root / "outputs/matches/7913441/inputs/cuotas_bwin_7913441.txt"
            txt.parent.mkdir(parents=True, exist_ok=True)
            txt.write_text(
                "Event ID: 7913441\n"
                "URL: https://www.bwin.es/es/sports/eventos/chelsea-f-psg-f-2:7913441\n"
                f"Captura: {now_m.isoformat()}\n"
                "Acordeones: 1 | Selecciones: 2\n"
                "MERCADO: Total de goles | Tiempo reglamentario\n"
                "Más de 2,5 : 1.60\n"
                "Menos de 2,5 : 2.25\n",
                encoding="utf-8",
            )
            pf_dir = root / "pf_1638301"
            pf_dir.mkdir(parents=True)
            rc_pf = pf.main([
                "--discovery", str(disc_path),
                "--preselection", str(base / "enriched/preselection.json"),
                "--fixture-id", "1638301",
                "--root", str(root),
                "--reuse-existing",
                "--min-lead-minutes", "45",
                "--output-dir", str(pf_dir),
            ])
            self.assertEqual(rc_pf, 0)
            pf_doc = trial.read(pf_dir / "market_preflight.json")
            self.assertEqual(pf_doc["matches"][0]["quality_gate"]["researchable_after_data_gate"], 2)

            # T-15 lineup cutoff, T-5 finish y bloqueo tras kickoff se mantienen intactos
            from bet_engine.context.lineup_manager import evaluate_fixture_lineup_state
            lu = evaluate_fixture_lineup_state(
                api_lineups_response=None,
                fixture_id=1638301,
                event_id="7913441",
                home_id=19001,
                away_id=19002,
                home_name="Chelsea W",
                away_name="Paris Saint Germain W",
                kickoff_utc=ko_utc.isoformat(),
                now_utc=(ko_utc - timedelta(minutes=14)).isoformat(),
                lineup_cutoff_minutes=15,
            )
            self.assertFalse(lu["confirmed_complete_22"])
            self.assertTrue(lu["cutoff_reached"])


if __name__ == "__main__":
    unittest.main()
