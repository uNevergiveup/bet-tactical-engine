import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bet_engine.context.evidence_aggregator import (
    PLAYER_STATUS_INSUFFICIENT_SAMPLE,
    PLAYER_STATUS_NO_DATA,
    PLAYER_STATUS_NOT_RECORDED,
    PLAYER_STATUS_ZERO_RECORDED,
    build_statistical_evidence_bundle,
)
from bet_engine.markets.bwin_catalog import check_equivalence_forbidden, extract_bwin_catalog
from bet_engine.markets.tactical_market_comparator import (
    MarketComparisonError,
    build_comparison_option,
    build_temporal_control_audit,
    compare_option_pair,
    compute_implied_break_even,
    evaluate_price_validity,
    finalize_market_comparison,
    prepare_market_comparison_package,
)
from bet_engine.markets.tactical_market_matcher import match_hypotheses_to_catalog


def _write_bwin_txt(path: Path, body: str, event_id: str = "7901446") -> Path:
    path.write_text(f"Event ID: {event_id}\n\n{body}", encoding="utf-8")
    return path


class TestPhase2MarketIntelligence(unittest.TestCase):
    def test_1_hypothesis_without_equivalent_market_is_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            txt = _write_bwin_txt(Path(td) / "b.txt", "MERCADO: Resultado del partido | Tiempo reglamentario\nSao Paulo : 2.10\nX : 3.20\nFluminense : 3.50\n")
            cat = extract_bwin_catalog(txt, event_id="7901446", fixture_id=1492457, home_name="Sao Paulo", away_name="Fluminense")
            scout = {
                "identity": {"fixture_id": 1492457, "event_id": "7901446", "home": "Sao Paulo", "away": "Fluminense"},
                "analysis": {"hypotheses": [{"tactical_mechanism": "Presion en banda", "related_market_family": "saques_de_banda_equipo"}]},
            }
            matched = match_hypotheses_to_catalog(scout_report=scout, bwin_catalog=cat)
            self.assertEqual(matched["links"][0]["match_status"], "UNSUPPORTED_MARKET")

    def test_2_and_3_and_5_cards_headers_and_settlement_unverified_not_no_odds(self):
        with tempfile.TemporaryDirectory() as td:
            body = (
                "MERCADO: Número de tarjetas en total | Tiempo reglamentario\n"
                "Más de 4,5 : 1.82\nMenos de 4,5 : 1.90\n"
                "MERCADO: Total de tarjetas | Tiempo reglamentario\n"
                "Más de 3,5 : 1.45\n"
                "MERCADO: Total tarjetas\n"
                "Más de 5,5 : 2.40\n"
                "MERCADO: Tarjetas en total\n"
                "Más de 6,5 : 3.40\n"
                "MERCADO: Total de córners | Tiempo reglamentario\n"
                "Más de 9,5 : 1.66\n"
                "MERCADO: Total de córners | 1ª parte\n"
                "Más de 4,5 : 1.95\n"
            )
            txt = _write_bwin_txt(Path(td) / "cards.txt", body)
            cat = extract_bwin_catalog(txt, event_id="7901446", fixture_id=1492457, home_name="Sao Paulo", away_name="Fluminense")
            cards = [e for e in cat["entries"] if e["market_family"] == "CARDS_TOTAL"]
            self.assertEqual(len(cards), 5)
            over45 = next(e for e in cards if e["canonical_key"] == "over_4_5_cards")
            self.assertEqual(over45["odd"], "1.82")
            self.assertEqual(over45["settlement_status"], "SETTLEMENT_UNVERIFIED")
            self.assertEqual(over45["commercial_coverage"], "SETTLEMENT_UNVERIFIED")
            corners_ft = [e for e in cat["entries"] if e["canonical_key"] == "over_9_5_corners"]
            self.assertEqual(corners_ft[0]["commercial_coverage"], "OFFERED_AND_MODELED")
            corners_h1 = [e for e in cat["entries"] if e["canonical_key"] == "h1_over_4_5_corners"]
            self.assertEqual(corners_h1[0]["commercial_coverage"], "OFFERED_NOT_MODELED")

    def test_4_commercial_duplicates_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            body = (
                "MERCADO: Total de goles | Tiempo reglamentario\n"
                "Más de 2,5 : 2.10\n"
                "Más de 2,5 : 2.10\n"
            )
            txt = _write_bwin_txt(Path(td) / "dup.txt", body)
            cat = extract_bwin_catalog(txt, event_id="7901446", fixture_id=1492457)
            self.assertEqual(cat["total_selections"], 1)
            self.assertEqual(cat["duplicate_selections_skipped"], 1)

    def test_6_to_8_forbidden_equivalences_blocked(self):
        self.assertIsNotNone(check_equivalence_forbidden("CORNERS_TEAM", "CORNERS_TOTAL"))
        self.assertIsNotNone(check_equivalence_forbidden("SHOTS", "ON_TARGET"))
        self.assertIsNotNone(check_equivalence_forbidden("FOULS_COMMITTED", "FOULS_DRAWN"))
        self.assertIsNotNone(check_equivalence_forbidden("CARDS_TOTAL", "CARD_POINTS"))
        self.assertIsNotNone(check_equivalence_forbidden("YELLOW_CARDS", "CARDS_WITH_RED"))
        self.assertIsNotNone(check_equivalence_forbidden("PLAYER_SHOTS_2_PLUS", "OVER_2_5_SHOTS"))
        self.assertIsNotNone(check_equivalence_forbidden("PLAYER_SUB", "PLAYER_STARTER"))
        self.assertIsNotNone(check_equivalence_forbidden("GOALS_OVER_2_5", "BTTS_YES"))

    def test_9_10_12_13_19_evidence_aggregator_null_vs_zero_red_cards_and_cutoff(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "raw.json"
            payload = {
                "response": [
                    {
                        "fixture": {"id": 1001, "date": "2026-11-10T20:00:00Z", "status": {"short": "FT"}},
                        "teams": {"home": {"id": 126, "name": "Sao Paulo"}, "away": {"id": 200, "name": "Rival A"}},
                        "goals": {"home": 1, "away": 0},
                        "events": [{"type": "Card", "detail": "Red Card"}],
                        "statistics": [
                            {"team": {"id": 126}, "statistics": [{"type": "Corner Kicks", "value": 6}]},
                            {"team": {"id": 200}, "statistics": [{"type": "Corner Kicks", "value": 4}]},
                        ],
                        "players": [
                            {
                                "team": {"id": 126},
                                "players": [
                                    {
                                        "player": {"id": 9, "name": "Calleri"},
                                        "statistics": [{"games": {"minutes": 40, "substitute": False}, "shots": {"total": 0, "on": None}, "fouls": {"committed": None}}],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "fixture": {"id": 9999, "date": "2026-11-25T20:00:00Z", "status": {"short": "FT"}},  # Post-kickoff!
                        "teams": {"home": {"id": 126, "name": "Sao Paulo"}, "away": {"id": 201, "name": "Rival B"}},
                        "goals": {"home": 5, "away": 5},
                        "events": [],
                        "statistics": [],
                        "players": [],
                    },
                ]
            }
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
            bundle = build_statistical_evidence_bundle(
                home_id=126,
                away_id=124,
                kickoff_utc="2026-11-20T00:30:00Z",
                raw_cache_sources=[{"path": str(cache_file)}],
            )
            self.assertEqual(bundle["raw_fixtures_loaded"], 1)  # Post-kickoff fixture excluded
            corners_all = bundle["home_distributions"]["metrics"]["corners"]["own"]["all"]
            self.assertEqual(corners_all["red_card_distorted_count"], 1)
            calleri = bundle["players"]["home"]["calleri"]
            self.assertEqual(calleri["exposure_status"], PLAYER_STATUS_INSUFFICIENT_SAMPLE)
            self.assertEqual(calleri["shots"]["status"], PLAYER_STATUS_ZERO_RECORDED)
            self.assertEqual(calleri["on_target"]["status"], PLAYER_STATUS_NOT_RECORDED)
            self.assertEqual(calleri["fouls_committed"]["status"], PLAYER_STATUS_NOT_RECORDED)

    def test_14_15_16_19_21_22_price_validity_and_implied_prob(self):
        imp = compute_implied_break_even("1.82")
        self.assertEqual(imp, Decimal("0.5495"))
        expired = evaluate_price_validity(
            captured_at_utc="2026-11-19T12:00:00Z",
            asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            kickoff_utc=datetime(2026, 11, 20, 0, 30, tzinfo=timezone.utc),
            max_age_minutes=180,
        )
        self.assertEqual(expired["status"], "PRICE_EXPIRED")
        post_ko = evaluate_price_validity(
            captured_at_utc="2026-11-20T01:00:00Z",
            asof_utc=datetime(2026, 11, 20, 1, 10, tzinfo=timezone.utc),
            kickoff_utc=datetime(2026, 11, 20, 0, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(post_ko["status"], "PRICE_INVALID_POST_KICKOFF")

        opt = build_comparison_option(
            {"selection_id": "abc", "market_family": "CORNERS_TEAM", "canonical_key": "home_over_4_5_corners", "odd": "1.51", "settlement_status": "VERIFIED_STANDARD"},
            price_validity={"status": "VALID_PREMATCH"},
        )
        self.assertEqual(opt["predictive_probability_status"], "PREDICTIVE_PROBABILITY_NOT_ACCREDITED")
        self.assertIsNone(opt["accredited_p_model"])

        with tempfile.TemporaryDirectory() as td:
            empty_txt = _write_bwin_txt(Path(td) / "empty.txt", "MERCADO: Total de goles | Tiempo reglamentario\n")
            cat = extract_bwin_catalog(empty_txt, event_id="1", fixture_id=1)
            self.assertEqual(cat["entries"][0]["commercial_coverage"], "PRICE_UNAVAILABLE")

    def test_11_18_20_23_comparator_validation_and_no_bet_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            scout = {
                "identity": {"fixture_id": 1492457, "event_id": "7901446", "dossier_id": "7901446", "home": "Sao Paulo", "away": "Fluminense", "kickoff_utc": "2026-11-20T00:30:00Z"},
                "analysis": {"hypotheses": [{"tactical_mechanism": "Corners por banda", "related_market_family": "corners_local", "evidence_quality": "PROBABLE", "pending_info": "XI oficial"}]},
            }
            txt = _write_bwin_txt(Path(td) / "b.txt", "MERCADO: Sao Paulo - Total de córners | Tiempo reglamentario\nMás de 4,5 : 1.51\n")
            cat = extract_bwin_catalog(txt, event_id="7901446", fixture_id=1492457, home_name="Sao Paulo", away_name="Fluminense", captured_at_utc="2026-11-19T23:45:52Z")
            matched = match_hypotheses_to_catalog(scout_report=scout, bwin_catalog=cat)
            prep = prepare_market_comparison_package(
                scout_report=scout,
                bwin_catalog=cat,
                matcher_result=matched,
                evidence_bundle=None,
                output_dir=Path(td) / "out",
                asof_utc=datetime(2026, 11, 19, 23, 50, tzinfo=timezone.utc),
            )
            # Invalid JSON with actionable_bet=True must be rejected
            with self.assertRaises(MarketComparisonError):
                finalize_market_comparison(
                    comp_input=prep["comparison_input"],
                    ai_evaluation={"actionable_bet": True, "exploratory_candidates": []},
                    output_dir=Path(td) / "out",
                )

            # Valid exploratory evaluation preserves formal_bet_authorized=False
            valid_ai = {
                "comparisons": [{"pair_label": "home_over_4_5_corners", "preferred_option_or_indeterminate": "INDETERMINATE_NO_CLEAR_VALUE_WINNER"}],
                "exploratory_candidates": [{"selection_id": cat["entries"][0]["selection_id"], "canonical_key": "home_over_4_5_corners", "state": "LINEUP_DEPENDENCY_PENDING", "rationale": "Falta XI oficial"}],
                "overall_conclusion": "Pendiente de XI.",
            }
            fin = finalize_market_comparison(comp_input=prep["comparison_input"], ai_evaluation=valid_ai, output_dir=Path(td) / "out")
            self.assertFalse(fin["report"]["formal_bet_authorized"])
            self.assertEqual(fin["report"]["no_bet_and_operational_ledger"]["formal_authorized_bets"], 0)
            self.assertGreaterEqual(fin["report"]["no_bet_and_operational_ledger"]["detailed_breakdown"]["ALINEACION_PENDIENTE"], 1)

            audit = build_temporal_control_audit(
                analysis_cutoff_utc="2026-11-19T23:35:00Z",
                execution_utc=datetime(2026, 11, 19, 23, 55, tzinfo=timezone.utc),
                captured_at_utc="2026-11-19T23:45:52Z",
                kickoff_utc=datetime(2026, 11, 20, 0, 30, tzinfo=timezone.utc),
            )
            self.assertTrue(audit["productive_rules_preserved"]["T_45_rule_active"])
            self.assertTrue(audit["productive_rules_preserved"]["T_45_would_block_productive_bet"])


if __name__ == "__main__":
    unittest.main()
