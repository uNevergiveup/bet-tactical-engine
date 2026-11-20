import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.analytics.early_tactical_scout import (
    finalize_early_scout,
    prepare_early_scout_package,
)
from bet_engine.context.evidence_aggregator import (
    PLAYER_STATUS_INSUFFICIENT_SAMPLE,
    PLAYER_STATUS_NO_DATA,
    build_player_fouls_duel_context,
    build_player_shots_context,
    build_statistical_evidence_bundle,
)
from bet_engine.markets.bwin_catalog import extract_bwin_catalog
from bet_engine.markets.tactical_market_comparator import (
    MarketComparisonError,
    evaluate_prematch_temporal_policy,
    finalize_market_comparison,
    prepare_market_comparison_package,
    refresh_expedient_with_lineups,
)
from bet_engine.markets.tactical_market_matcher import match_hypotheses_to_catalog


def _make_api_lineup_payload(home_id: int, away_id: int, home_name: str, away_name: str) -> list[dict]:
    def _team_block(tid: int, tname: str, base_pid: int) -> dict:
        return {
            "team": {"id": tid, "name": tname},
            "formation": "4-2-3-1",
            "startXI": [{"player": {"id": base_pid + i, "name": f"{tname} Player {i}", "number": i, "pos": "M", "grid": "2:1"}} for i in range(1, 12)],
            "substitutes": [{"player": {"id": base_pid + 20, "name": f"{tname} Sub 20", "number": 20, "pos": "F"}}],
            "coach": {"id": base_pid + 99, "name": f"Coach {tname}"},
        }
    return [_team_block(home_id, home_name, 1000), _team_block(away_id, away_name, 2000)]


class TestPhase3PilotAndFouls(unittest.TestCase):
    def test_1_end_to_end_independent_fixture_without_external_report(self):
        """Prueba completa con partido distinto de Sao Paulo-Fluminense y sin informe externo."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_file = root / "raw_alpha_beta.json"
            raw_payload = {
                "response": [
                    {
                        "fixture": {"id": 8001, "date": "2026-11-10T18:00:00Z", "status": {"short": "FT"}},
                        "teams": {"home": {"id": 501, "name": "Alpha FC"}, "away": {"id": 601, "name": "Rival X"}},
                        "goals": {"home": 2, "away": 1},
                        "events": [],
                        "statistics": [
                            {"team": {"id": 501}, "statistics": [{"type": "Corner Kicks", "value": 7}, {"type": "Total Shots", "value": 15}, {"type": "Fouls", "value": 12}]},
                            {"team": {"id": 601}, "statistics": [{"type": "Corner Kicks", "value": 3}, {"type": "Total Shots", "value": 8}, {"type": "Fouls", "value": 14}]},
                        ],
                        "players": [
                            {
                                "team": {"id": 501},
                                "players": [
                                    {
                                        "player": {"id": 5011, "name": "Alpha FC Player 6"},
                                        "statistics": [{
                                            "games": {"minutes": 90, "position": "M", "substitute": False},
                                            "fouls": {"committed": 3, "drawn": 1},
                                            "dribbles": {"attempts": 1, "success": 1, "past": 1},
                                            "cards": {"yellow": 0, "red": 0},
                                            "shots": {"total": 1, "on": 0},
                                        }],
                                    }
                                ],
                            },
                            {
                                "team": {"id": 601},
                                "players": [],
                            },
                        ],
                    }
                ]
            }
            cache_file.write_text(json.dumps(raw_payload), encoding="utf-8")

            bwin_txt = root / "bwin_9001.txt"
            bwin_txt.write_text(
                "Event ID: 9001\n\n"
                "MERCADO: Alpha FC - Total de córners | Tiempo reglamentario\n"
                "Más de 4,5 : 1.72\n"
                "MERCADO: Jugador - Total de faltas (2+)\n"
                "Alpha FC Player 6 : 1.85\n"
                "MERCADO: Crack bwin NUEVO (5+)\n"
                "Alpha FC Player 6 : 3.10\n",
                encoding="utf-8",
            )

            ident = {
                "fixture_id": 55001,
                "event_id": "9001",
                "home": "Alpha FC",
                "away": "Beta United",
                "country": "Spain",
                "league": "La Liga",
                "kickoff_utc": "2026-11-20T20:00:00Z",
                "kickoff_madrid": "2026-11-20T21:00:00+01:00",
            }
            prep = prepare_early_scout_package(
                identity=ident,
                output_dir=root / "scout",
                external_reports=[],
                asof_utc=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc),
            )
            self.assertIn("NO_EXTERNAL_QUALITATIVE_REPORT", prep["scout_input"]["coverage"]["warnings"])

            ana = {
                "coaches_and_systems": {"home": "4-2-3-1", "away": "4-4-2"},
                "players": {"watch": ["Alpha FC Player 6"]},
                "tactical_matchup": {"mechanism": "Presion en medio campo de Alpha FC"},
                "competitive_context": {"note": "Liga regular"},
                "hypotheses": [
                    {
                        "tactical_mechanism": "Faltas tacticas de Alpha FC Player 6 en transicion y corners locales.",
                        "team_and_players": "Alpha FC Player 6",
                        "necessary_conditions": "Titularidad confirmada",
                        "supporting_stats": "1 partido historico en cache (muestra insuficiente para per-90)",
                        "contradictory_evidence": "Muestra n=1 insuficiente y sin informe cualitativo externo",
                        "opponent_coach_response": "Beta United juega directo evitando el carril central",
                        "invalidation_scenarios": "Suplencia de Player 6",
                        "related_market_family": "faltas_y_corners_local",
                        "evidence_quality": "INFERIDO",
                        "pending_info": "XI oficial y mas muestra",
                    }
                ],
            }
            fin_scout = finalize_early_scout(scout_input=prep["scout_input"], analysis=ana, output_dir=root / "scout")
            cat = extract_bwin_catalog(bwin_txt, event_id="9001", fixture_id=55001, home_name="Alpha FC", away_name="Beta United", captured_at_utc="2026-11-20T18:10:00Z")
            self.assertEqual(cat["counts_by_commercial_coverage"]["MAPPING_UNRESOLVED"], 1)  # Crack bwin
            self.assertEqual(cat["counts_by_commercial_coverage"]["SETTLEMENT_UNVERIFIED"], 1)  # Player fouls
            self.assertEqual(cat["counts_by_commercial_coverage"]["OFFERED_AND_MODELED"], 1)  # Alpha FC corners

            ev_bundle = build_statistical_evidence_bundle(
                home_id=501,
                away_id=502,
                kickoff_utc="2026-11-20T20:00:00Z",
                raw_cache_sources=[{"path": str(cache_file)}],
            )
            matched = match_hypotheses_to_catalog(scout_report=fin_scout["report"], bwin_catalog=cat, evidence_bundle=ev_bundle)
            comp_prep = prepare_market_comparison_package(
                scout_report=fin_scout["report"],
                bwin_catalog=cat,
                matcher_result=matched,
                evidence_bundle=ev_bundle,
                output_dir=root / "cmp",
                asof_utc=datetime(2026, 11, 20, 18, 20, tzinfo=timezone.utc),
            )
            self.assertEqual(len(comp_prep["comparison_input"]["evidence_summary"]["player_fouls_duels"]), 1)
            duel0 = comp_prep["comparison_input"]["evidence_summary"]["player_fouls_duels"][0]
            # Sample n=1 < 3 -> INSUFFICIENT_EXPOSURE_FOR_PER90
            self.assertEqual(duel0["player_defensive"]["exposure_status"], PLAYER_STATUS_INSUFFICIENT_SAMPLE)
            self.assertEqual(duel0["player_defensive"]["fouls_per_90_status"], "INSUFFICIENT_EXPOSURE_FOR_PER90")
            # Missing opponent dribbles -> NO_VERIFICADO
            self.assertEqual(duel0["opponent_profile"]["dribbles_status"], "NO_VERIFICADO")
            self.assertEqual(duel0["tactical_matchup"]["marking_scheme"], "NO_VERIFICADO")

    def test_2_fouls_duels_role_changes_and_shots_limited_scope(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "multi.json"
            fixtures = []
            for idx, pos in enumerate(["M", "D", "M"], start=1):
                fixtures.append({
                    "fixture": {"id": 900 + idx, "date": f"2026-11-0{idx}T18:00:00Z", "status": {"short": "FT"}},
                    "teams": {"home": {"id": 10, "name": "Home"}, "away": {"id": 20, "name": "Away"}},
                    "goals": {"home": 1, "away": 1},
                    "events": [],
                    "statistics": [],
                    "players": [{
                        "team": {"id": 10},
                        "players": [{
                            "player": {"id": 77, "name": "Mid Hybrid"},
                            "statistics": [{
                                "games": {"minutes": 80, "position": pos, "substitute": False},
                                "fouls": {"committed": 2, "drawn": 5},
                                "shots": {"total": 3, "on": 1},
                                "cards": {"yellow": 0, "red": 0},
                            }],
                        }],
                    }],
                })
            cache_file.write_text(json.dumps({"response": fixtures}), encoding="utf-8")
            bundle = build_statistical_evidence_bundle(home_id=10, away_id=20, kickoff_utc="2026-11-20T20:00:00Z", raw_cache_sources=[{"path": str(cache_file)}])
            duel = build_player_fouls_duel_context(evidence_bundle=bundle, player_name="Mid Hybrid", side="home")
            self.assertTrue(duel["player_defensive"]["role_changes_detected"])
            # Fouls committed (2) must NOT be confused with fouls drawn (5)
            self.assertEqual(duel["player_defensive"]["fouls_committed"]["mean"], 2.0)

            shots_ctx = build_player_shots_context(evidence_bundle=bundle, player_name="Mid Hybrid", side="home")
            self.assertEqual(shots_ctx["future_phase_note"], "PLAYER_SHOT_OPPOSITION_ANALYSIS")
            self.assertEqual(shots_ctx["shots"]["mean"], 3.0)
            self.assertEqual(shots_ctx["on_target"]["mean"], 1.0)

    def test_3_t45_revised_temporal_policy_all_checkpoints(self):
        ko = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
        scout_t = ko - timedelta(minutes=90)
        eval_t = ko - timedelta(minutes=65)

        for lead_m, exp_exp, exp_formal in [
            (60, True, True),
            (45, True, True),
            (30, True, True),
            (15, True, True),  # T-15 is NOT a betting close when XI & fresh price verified!
            (5, True, True),
            (0, False, False),  # Kickoff blocked
            (-10, False, False),  # Post-kickoff blocked
        ]:
            dec_t = ko - timedelta(minutes=lead_m)
            cap_t = dec_t - timedelta(minutes=5)
            pol = evaluate_prematch_temporal_policy(
                scouting_start_utc=scout_t,
                evaluation_start_utc=eval_t,
                formal_decision_utc=dec_t,
                kickoff_utc=ko,
                bwin_captured_at_utc=cap_t,
                target_status="NS" if lead_m > 0 else "1H",
                lineup_status="LINEUPS_API_CONFIRMED",
                identity_consistent=True,
                predictive_calibration_accredited=True,
                commercial_settlement_verified=True,
            )
            self.assertEqual(pol["exploratory_allowed"], exp_exp, msg=f"lead={lead_m}")
            self.assertEqual(pol["formal_evaluation_eligible"], exp_formal, msg=f"lead={lead_m}")

        # Without official XI or calibration, exploratory is allowed at T-30 but formal BET is blocked
        pol_no_xi = evaluate_prematch_temporal_policy(
            scouting_start_utc=scout_t,
            evaluation_start_utc=eval_t,
            formal_decision_utc=ko - timedelta(minutes=30),
            kickoff_utc=ko,
            bwin_captured_at_utc=ko - timedelta(minutes=35),
            lineup_status="LINEUPS_PROBABLE_ONLY",
            predictive_calibration_accredited=False,
            commercial_settlement_verified=False,
        )
        self.assertTrue(pol_no_xi["exploratory_allowed"])
        self.assertFalse(pol_no_xi["formal_bet_authorized"])
        self.assertIn("OFFICIAL_XI_REQUIRED(LINEUPS_PROBABLE_ONLY)", pol_no_xi["formal_blocks"])
        self.assertIn("PREDICTIVE_CALIBRATION_PENDING", pol_no_xi["formal_blocks"])

    def test_4_official_lineup_refresh_preserves_early_hypotheses_and_blocks_live(self):
        ko = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
        scout_rep = {
            "analysis": {
                "hypotheses": [
                    {"tactical_mechanism": "Ataque por banda", "team_and_players": "Alpha FC Player 6, Alpha FC Missing"},
                ]
            }
        }
        api_lu = _make_api_lineup_payload(501, 502, "Alpha FC", "Beta United")
        ref = refresh_expedient_with_lineups(
            scout_report=scout_rep,
            fixture_id=55001,
            event_id="9001",
            home_id=501,
            away_id=502,
            home_name="Alpha FC",
            away_name="Beta United",
            kickoff_utc=ko,
            now_utc=ko - timedelta(minutes=30),
            api_lineups_response=api_lu,
            watch_players=["Alpha FC Player 6", "Alpha FC Sub 20", "Alpha FC Missing"],
        )
        self.assertTrue(ref["official_confirmed_22"])
        self.assertEqual(ref["initial_hypotheses_count"], 1)
        roles = {r["player"]: r["role"] for r in ref["player_exposure_roles"]}
        self.assertEqual(roles["Alpha FC Player 6"], "TITULAR")
        self.assertEqual(roles["Alpha FC Sub 20"], "SUPLENTE")
        self.assertEqual(roles["Alpha FC Missing"], "AUSENTE")
        self.assertEqual(len(ref["discarded_hypotheses"]), 1)

        with self.assertRaises(MarketComparisonError):
            refresh_expedient_with_lineups(
                scout_report=scout_rep,
                fixture_id=55001,
                event_id="9001",
                home_id=501,
                away_id=502,
                home_name="Alpha FC",
                away_name="Beta United",
                kickoff_utc=ko,
                now_utc=ko + timedelta(minutes=5),
                target_status="1H",
            )


if __name__ == "__main__":
    unittest.main()
