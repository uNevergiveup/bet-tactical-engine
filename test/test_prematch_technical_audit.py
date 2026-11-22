import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bet_engine.context.research_parser import parse_research_identity_and_metadata
from bet_engine.markets.tactical_market_comparator import (
    MarketComparisonError,
    build_no_bet_ledger,
    finalize_market_comparison,
    prepare_market_comparison_package,
    refresh_expedient_with_lineups,
)
from prematch_steps import _load_current_lineup_refresh


class TechnicalAuditTests(unittest.TestCase):
    kickoff = "2026-11-21T17:45:00+00:00"
    refresh_time = "2026-11-21T17:30:00+00:00"

    def make_refresh(self, ambiguous=False):
        teams = []
        for team_id, name in [(253, "Dundee"), (254, "Heart Of Midlothian")]:
            xi = [{"player": {"id": team_id * 100 + i, "name": f"Jugador {team_id} {i}"}} for i in range(11)]
            if team_id == 253:
                xi[0]["player"]["name"] = "E. Hamilton"
            elif ambiguous:
                xi[0]["player"]["name"] = "Ethan Hamilton"
            teams.append({"team": {"id": team_id, "name": name}, "startXI": xi})
        return refresh_expedient_with_lineups(
            scout_report={}, fixture_id=1556707, event_id="7911406",
            home_id=253, away_id=254, home_name="Dundee", away_name="Heart Of Midlothian",
            kickoff_utc=self.kickoff, now_utc=self.refresh_time, api_lineups_response=teams,
            watch_players=["Ethan Hamilton", "25300", "Otro Jugador"],
        )

    def test_lineup_abbreviation_id_and_ambiguity(self):
        refresh = self.make_refresh()
        self.assertEqual([p["role"] for p in refresh["player_exposure_roles"]],
                         ["TITULAR", "TITULAR", "EXPOSICION_INCIERTA"])
        self.assertFalse(refresh["lineup_evaluation"]["primary_official_verified"])
        self.assertTrue(refresh["lineup_gate_passed"])
        role = self.make_refresh(ambiguous=True)["player_exposure_roles"][0]
        self.assertEqual(role["reason"], "AMBIGUOUS_PLAYER")

    def test_refresh_is_scoped_and_price_age_is_independent(self):
        refresh = self.make_refresh()
        now = datetime(2026, 11, 21, 17, 32, tzinfo=timezone.utc)
        ident = {"fixture_id": 1556707, "event_id": "7911406", "kickoff_utc": self.kickoff,
                 "home": "Dundee", "away": "Heart Of Midlothian"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lineup.json"
            path.write_text(json.dumps({**refresh, "eval_dir": folder}), encoding="utf-8")
            target = {**ident, "bwin_event_id": "7911406"}
            self.assertIsNone(_load_current_lineup_refresh(path, Path(folder) / "other", target, now))
            kwargs = dict(scout_report={"identity": ident},
                          bwin_catalog={"captured_at_utc": "2026-11-21T15:12:52Z"},
                          matcher_result={}, evidence_bundle=None, output_dir=Path(folder), asof_utc=now)
            data = prepare_market_comparison_package(**kwargs)["comparison_input"]
            self.assertEqual(data["temporal_audit"]["lineup_status"], "LINEUPS_PENDING")
            data = prepare_market_comparison_package(**kwargs, lineup_refresh=refresh)["comparison_input"]
            self.assertEqual(data["temporal_audit"]["lineup_status"], "LINEUPS_API_CONFIRMED")
            self.assertEqual(data["price_validity"]["exploratory_price_status"], "VALID_PREMATCH")
            self.assertEqual(data["price_validity"]["formal_price_status"], "PRICE_EXPIRED")
            with self.assertRaisesRegex(MarketComparisonError, "CURRENT_LINEUP_REFRESH"):
                finalize_market_comparison(comp_input=data, ai_evaluation={}, output_dir=Path(folder))
            with self.assertRaises(MarketComparisonError):
                prepare_market_comparison_package(**kwargs, lineup_refresh={**refresh, "event_id": "other"})

    def test_no_bet_category_counted_once_per_candidate(self):
        candidate = {"state": "LINEUP_DEPENDENCY_PENDING", "no_bet_category": "ALINEACION_PENDIENTE"}
        ledger = build_no_bet_ledger(comp_input={}, ai_doc={"exploratory_candidates": [candidate, candidate]})
        self.assertEqual(ledger["detailed_breakdown"]["ALINEACION_PENDIENTE"], 2)

    def test_markdown_citations_flag_placeholder(self):
        sources = parse_research_identity_and_metadata(
            "[dato](http://docs.google.com/unknown_url)\n[cita]: https://example.org/article"
        )
        self.assertEqual(sources["citation_urls"], ["https://example.org/article"])
        self.assertEqual(sources["invalid_citation_urls"], ["http://docs.google.com/unknown_url"])
