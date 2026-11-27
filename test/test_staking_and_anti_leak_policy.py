"""Pruebas unitarias e integrales de la política anti-fugas, devigging,
estimación de Player Props por 90', desduplicación de correlación intra-partido
y dimensionamiento de Stake (Kelly Fraccional) en lotes horarios.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bet_engine.markets.staking_and_policy import (
    allocate_match_portfolio_stakes,
    compute_market_devig_map,
    evaluate_collective_quant_policy,
    evaluate_player_prop_quant,
    extract_min_acceptable_odd,
    is_cross_league_club_competition,
)


class TestStakingAndAntiLeakPolicy(unittest.TestCase):
    def test_devigging_extracts_fair_probs_and_overround(self) -> None:
        odds = {
            "over_2_5_goals": 1.90,
            "under_2_5_goals": 1.90,
            "home_over_4_5_corners": 1.85,
            "home_under_4_5_corners": 1.80,
            "home_win": 2.10,
            "draw": 3.40,
            "away_win": 3.50,
        }
        devig = compute_market_devig_map(odds)
        self.assertAlmostEqual(devig["over_2_5_goals"]["p_market_fair"], 0.50, places=4)
        self.assertAlmostEqual(devig["over_2_5_goals"]["overround"], 0.0526, places=3)
        self.assertAlmostEqual(devig["home_under_4_5_corners"]["overround"], 0.0961, places=3)
        self.assertAlmostEqual(
            devig["home_win"]["p_market_fair"] + devig["draw"]["p_market_fair"] + devig["away_win"]["p_market_fair"],
            1.0,
            places=3,
        )

    def test_extract_min_acceptable_odd_from_explicit_and_spanish_text(self) -> None:
        self.assertEqual(extract_min_acceptable_odd({"min_odd": 1.72}), 1.72)
        self.assertEqual(
            extract_min_acceptable_odd({"lineup_condition": "Invalidar ante una caída de precio por debajo de 1,65."}),
            1.65,
        )
        self.assertEqual(
            extract_min_acceptable_odd({"lineup_condition": "Se descartaría ante cuota inferior a 1.72."}),
            1.72,
        )

    def test_three_empirical_leaks_are_blocked_automatically(self) -> None:
        # Fuga 1: Córners UNDER con muestra corta (n=7 < 14) -> BLOQUEADO aunque EV central sea alto
        corners_u = evaluate_collective_quant_policy(
            market_family="CORNERS_TEAM",
            direction="UNDER",
            temporal_scope="FT",
            odd=2.30,
            p_model=0.58,
            p_low=0.44,
            p_high=0.73,
            raw_sample_n=7,
            effective_sample_n=5.5,
        )
        self.assertFalse(corners_u["bet_authorized"])
        self.assertTrue(corners_u["leak_guard_blocked"])
        self.assertEqual(corners_u["raw_kelly_stake_pct"], 0.0)

        # Fuga 2: 1ª parte (H1) con muestra corta (n=6 < 14) -> BLOQUEADO
        h1_goals = evaluate_collective_quant_policy(
            market_family="GOALS_H1",
            direction="UNDER",
            temporal_scope="H1",
            odd=1.98,
            p_model=0.62,
            p_low=0.47,
            p_high=0.75,
            raw_sample_n=6,
        )
        self.assertFalse(h1_goals["bet_authorized"])
        self.assertTrue(h1_goals["leak_guard_blocked"])

        # Fuga 3: 1X2 / DNB en torneo inter-liga UEFA sin ajuste Elo (caso Olympiakos vs Milan / Viking)
        self.assertTrue(is_cross_league_club_competition("UEFA Europa League", "World"))
        dnb_uefa = evaluate_collective_quant_policy(
            market_family="DRAW_NO_BET",
            direction=None,
            temporal_scope="FT",
            odd=1.93,
            p_model=0.6198,
            p_low=0.3349,
            p_high=0.8482,
            raw_sample_n=17,
            is_cross_league=True,
        )
        self.assertFalse(dnb_uefa["bet_authorized"])
        self.assertTrue(dnb_uefa["leak_guard_blocked"])

    def test_goals_ft_and_min_odd_floor_work_as_expected(self) -> None:
        # Goles FT en liga doméstica con n=16 y EV=+12% -> BET autorizado con Stake Kelly
        goals_ok = evaluate_collective_quant_policy(
            market_family="GOALS_FT",
            direction="OVER",
            temporal_scope="FT",
            odd=1.75,
            p_model=0.65,
            p_low=0.54,
            p_high=0.75,
            raw_sample_n=16,
            effective_sample_n=13.5,
            devig_info={"p_market_fair": 0.545, "overround": 0.052},
            min_acceptable_odd=1.68,
        )
        self.assertTrue(goals_ok["bet_authorized"])
        self.assertGreaterEqual(goals_ok["raw_kelly_stake_pct"], 0.50)
        self.assertLessEqual(goals_ok["raw_kelly_stake_pct"], 2.00)

        # Mismo pick si la cuota cae a 1.63 por debajo del piso 1.65 (caso Aarhus) -> VETO inmediato
        goals_dropped = evaluate_collective_quant_policy(
            market_family="GOALS_FT",
            direction="OVER",
            temporal_scope="FT",
            odd=1.63,
            p_model=0.65,
            p_low=0.54,
            p_high=0.75,
            raw_sample_n=16,
            effective_sample_n=13.5,
            min_acceptable_odd=1.65,
        )
        self.assertFalse(goals_dropped["bet_authorized"])
        self.assertIn("CUOTA_BAJO_PISO_DECLARADO", goals_dropped["status"])

    def test_player_props_require_confirmed_starter_and_block_bench_or_high_odd_defender_sot(self) -> None:
        forward_evidence = {
            "player_id": 77,
            "side": "home",
            "metric": "total_shots",
            "threshold_at_least": 3,
            "n_valid": 8,
            "valid_minutes": 680,
            "hits": 7,
            "sample": [
                {"minutes": 85, "position": "F", "role": "STARTER", "value": 4}
                for _ in range(8)
            ],
        }
        # Pre-XI: pendiente XI (0 stake real, pero proyecta stake si es titular)
        pre_xi = evaluate_player_prop_quant(
            forward_evidence, odd=1.70, market_family="PLAYER_SHOTS", lineup_confirmed=False
        )
        self.assertFalse(pre_xi["bet_authorized"])
        self.assertEqual(pre_xi["status"], "CANDIDATO_PROP_PENDIENTE_XI")
        self.assertGreater(pre_xi["projected_stake_if_starter_pct"], 0.5)

        # Post-XI pero SUPLENTE (caso Mbeumo): veto inmediato
        bench = evaluate_player_prop_quant(
            forward_evidence, odd=1.70, market_family="PLAYER_SHOTS", lineup_role="SUPLENTE", lineup_confirmed=True
        )
        self.assertFalse(bench["bet_authorized"])
        self.assertIn("VETO_JUGADOR_NO_TITULAR", bench["status"])

        # Post-XI TITULAR (caso Braga / Brugué): BET autorizado con stake Kelly
        starter = evaluate_player_prop_quant(
            forward_evidence, odd=1.70, market_family="PLAYER_SHOTS", lineup_role="TITULAR", lineup_confirmed=True
        )
        self.assertTrue(starter["bet_authorized"])
        self.assertEqual(starter["status"], "BET_PROP_TITULAR_AUTORIZADO")
        self.assertGreaterEqual(starter["raw_kelly_stake_pct"], 0.50)

        # Defensa en 1+ SOT a cuota alta 2.40 (caso Renan Lodi): bloqueado por regla anti-fuga SOT defensa
        defender_sot = {
            "player_id": 88,
            "side": "home",
            "metric": "shots_on_target",
            "threshold_at_least": 1,
            "n_valid": 8,
            "valid_minutes": 700,
            "hits": 4,
            "sample": [{"minutes": 88, "position": "D", "role": "STARTER", "value": 1 if i < 4 else 0} for i in range(8)],
        }
        def_res = evaluate_player_prop_quant(
            defender_sot, odd=2.40, market_family="PLAYER_ON_TARGET", lineup_role="TITULAR", lineup_confirmed=True
        )
        self.assertFalse(def_res["bet_authorized"])
        self.assertIn("FUGA_SOT", def_res["status"])

        # Sesgo de selección por ceros omitidos como null (ej. 5 partidos con >=1 tiro y 3 ausentes sin ceros)
        censored_zeros = {
            "player_id": 99,
            "side": "away",
            "metric": "fouls_committed",
            "threshold_at_least": 2,
            "n_valid": 6,
            "n_missing_metric_in_observed_appearances": 3,
            "valid_minutes": 520,
            "hits": 6,
            "sample": [{"minutes": 87, "position": "F", "role": "STARTER", "value": 3} for _ in range(6)],
        }
        cens_res = evaluate_player_prop_quant(
            censored_zeros, odd=2.10, market_family="PLAYER_FOULS_COMMITTED", lineup_role="TITULAR", lineup_confirmed=True
        )
        self.assertFalse(cens_res["bet_authorized"])
        self.assertIn("SESGO_SELECCION_METRICA_INCOMPLETA", cens_res["status"])

    def test_intra_match_correlation_deduplication_and_portfolio_cap(self) -> None:
        """Verifica exactamente el escenario consultado por el usuario:
        Pick 1: Equipo A +1.5 goles (GOALS_AXIS)
        Pick 2: Equipo B +3.5 córners (CORNERS_AXIS, independiente)
        Pick 3: Total +3.5 goles (GOALS_AXIS, correlacionado con Pick 1 -> se descarta)
        """
        candidates = [
            {
                "selection_id": "pick_1",
                "market_header_literal": "Equipo A - Total de goles",
                "selection_literal": "Más de 1,5",
                "odd": 2.00,
                "quant_decision": {
                    "status": "BET_COLECTIVO_AUTORIZADO",
                    "bet_authorized": True,
                    "correlation_axis": "GOALS_AXIS",
                    "p_model": 0.58,
                    "fair_odd": 1.72,
                    "ev_model": 0.16,
                    "raw_kelly_stake_pct": 2.00,
                },
            },
            {
                "selection_id": "pick_2",
                "market_header_literal": "Equipo B - Total de córners",
                "selection_literal": "Más de 3,5",
                "odd": 1.85,
                "quant_decision": {
                    "status": "BET_COLECTIVO_AUTORIZADO",
                    "bet_authorized": True,
                    "correlation_axis": "CORNERS_AXIS",
                    "p_model": 0.61,
                    "fair_odd": 1.64,
                    "ev_model": 0.1285,
                    "raw_kelly_stake_pct": 1.50,
                },
            },
            {
                "selection_id": "pick_3",
                "market_header_literal": "Total de goles",
                "selection_literal": "Más de 3,5",
                "odd": 2.75,
                "quant_decision": {
                    "status": "BET_COLECTIVO_AUTORIZADO",
                    "bet_authorized": True,
                    "correlation_axis": "GOALS_AXIS",
                    "p_model": 0.40,
                    "fair_odd": 2.50,
                    "ev_model": 0.10,
                    "raw_kelly_stake_pct": 1.00,
                },
            },
        ]
        portfolio = allocate_match_portfolio_stakes(candidates, bankroll=1000.0, currency="EUR")
        self.assertEqual(portfolio["match_decision"], "BET")
        self.assertEqual(portfolio["authorized_picks_count"], 2)
        # Tope máximo del partido = 3.00% (30.00 EUR sobre bank de 1000 EUR)
        self.assertLessEqual(portfolio["total_match_stake_pct"], 3.00)
        self.assertLessEqual(portfolio["total_match_amount"], 30.00)

        by_id = {a["selection_id"]: a for a in portfolio["allocations"]}
        self.assertEqual(by_id["pick_1"]["decision"], "BET")
        self.assertEqual(by_id["pick_2"]["decision"], "BET")
        self.assertEqual(by_id["pick_3"]["decision"], "NO_BET_CORRELACIONADO")
        self.assertEqual(by_id["pick_3"]["stake_amount"], 0.0)


if __name__ == "__main__":
    unittest.main()
