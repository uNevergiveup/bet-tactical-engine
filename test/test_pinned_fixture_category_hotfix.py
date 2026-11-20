"""Pruebas de regresión del hotfix PINNED_FIXTURE_CATEGORY_UNCORROBORATED.

Cubre resolución de categoría en amistosos internacionales (league_id=10)
sin llamadas externas ni scraping.
"""

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import generate_context as gc

ROOT = Path(__file__).resolve().parent.parent


class PinnedFixtureCategoryHotfixTests(unittest.TestCase):
    def _make_match(
        self,
        *,
        event_id: int,
        home_name: str,
        away_name: str,
        kickoff_iso: str = "2026-11-18T00:00:00+01:00",
    ) -> gc.BwinMatch:
        return gc.BwinMatch(
            url=f"https://www.bwin.es/es/sports/eventos/test-2:{event_id}",
            event_id=event_id,
            home_name_raw=home_name,
            away_name_raw=away_name,
            home_name=home_name,
            away_name=away_name,
            home_gender="UNKNOWN",
            away_gender="UNKNOWN",
            event_date=kickoff_iso,
        )

    def _resolve(self, match: gc.BwinMatch, row: dict, ctx: dict):
        api = Mock()
        api.get.return_value = {"response": [row]}
        with patch.object(
            gc,
            "fixture_temporal_eligibility",
            return_value=(True, "FIXTURE_AND_BWIN_TIME_VERIFIED", "OK", {}),
        ):
            return gc.resolve_selected_fixture(match, api, ctx)

    def test_chile_colombia_7909714_real_selection_resolves(self) -> None:
        sel_path = ROOT / "outputs/matches/7909714/selection/selected_match.json"
        if not sel_path.is_file():
            self.skipTest("selected_match.json 7909714 no presente")
        selected = json.loads(sel_path.read_text(encoding="utf-8"))
        ctx = {
            "fixture_id": 1640519,
            "kickoff": datetime.fromisoformat("2026-11-17T23:00:00+00:00"),
            "selection": selected,
        }
        row = {
            "fixture": {"id": 1640519, "date": "2026-11-17T23:00:00+00:00", "status": {"short": "NS"}},
            "league": {
                "id": 10,
                "name": "Friendlies",
                "country": "World",
                "season": 2026,
                "round": "Friendly International",
            },
            "teams": {
                "home": {"id": 2383, "name": "Chile"},
                "away": {"id": 8, "name": "Colombia"},
            },
        }
        match = self._make_match(event_id=7909714, home_name="Chile", away_name="Colombia")
        home, away = self._resolve(match, row, ctx)
        self.assertEqual((home.id, away.id), (2383, 8))
        self.assertEqual((match.home_gender, match.away_gender), ("M", "M"))
        self.assertEqual(match.verified_fixture_id, 1640519)

    def test_argentina_costa_rica_7909713_resolves(self) -> None:
        ctx = {
            "fixture_id": 1647372,
            "kickoff": datetime.fromisoformat("2026-11-17T23:00:00+00:00"),
            "selection": {
                "home_api_id": 26,
                "away_api_id": 29,
                "league_id": 10,
                "season": 2026,
                "gender": "M",
            },
        }
        row = {
            "fixture": {"id": 1647372, "date": "2026-11-17T23:00:00+00:00", "status": {"short": "NS"}},
            "league": {
                "id": 10,
                "name": "Friendlies",
                "country": "World",
                "season": 2026,
                "round": "Friendly International",
            },
            "teams": {
                "home": {"id": 26, "name": "Argentina"},
                "away": {"id": 29, "name": "Costa Rica"},
            },
        }
        match = self._make_match(event_id=7909713, home_name="Argentina", away_name="Costa Rica")
        home, away = self._resolve(match, row, ctx)
        self.assertEqual((home.id, away.id), (26, 29))
        self.assertEqual((match.home_gender, match.away_gender), ("M", "M"))

    def test_womens_friendly_blocked_against_male_selection_and_allowed_if_female(self) -> None:
        base_ctx = {
            "fixture_id": 990001,
            "kickoff": datetime.fromisoformat("2026-11-17T23:00:00+00:00"),
            "selection": {
                "home_api_id": 8001,
                "away_api_id": 8002,
                "league_id": 10,
                "season": 2026,
                "gender": "M",
            },
        }
        row = {
            "fixture": {"id": 990001, "date": "2026-11-17T23:00:00+00:00", "status": {"short": "NS"}},
            "league": {
                "id": 10,
                "name": "Friendlies",
                "country": "World",
                "season": 2026,
                "round": "Friendly International",
            },
            "teams": {
                "home": {"id": 8001, "name": "Chile Women"},
                "away": {"id": 8002, "name": "Colombia Women"},
            },
        }
        match = self._make_match(event_id=7990001, home_name="Chile Women", away_name="Colombia Women")
        with self.assertRaisesRegex(ValueError, "PINNED_FIXTURE_CATEGORY_CONFLICT"):
            self._resolve(match, row, copy.deepcopy(base_ctx))

        female_ctx = copy.deepcopy(base_ctx)
        female_ctx["selection"]["gender"] = "F"
        home, away = self._resolve(match, row, female_ctx)
        self.assertEqual((home.id, away.id), (8001, 8002))
        self.assertEqual((match.home_gender, match.away_gender), ("F", "F"))

    def test_youth_u20_and_u23_friendlies_blocked(self) -> None:
        ctx = {
            "fixture_id": 990002,
            "kickoff": datetime.fromisoformat("2026-11-17T23:00:00+00:00"),
            "selection": {
                "home_api_id": 8101,
                "away_api_id": 8102,
                "league_id": 10,
                "season": 2026,
                "gender": "M",
            },
        }
        u20_row = {
            "fixture": {"id": 990002, "date": "2026-11-17T23:00:00+00:00", "status": {"short": "NS"}},
            "league": {
                "id": 10,
                "name": "Friendlies",
                "country": "World",
                "season": 2026,
                "round": "Friendly International",
            },
            "teams": {
                "home": {"id": 8101, "name": "Chile U20"},
                "away": {"id": 8102, "name": "Colombia U20"},
            },
        }
        match = self._make_match(event_id=7990002, home_name="Chile U20", away_name="Colombia U20")
        with self.assertRaisesRegex(ValueError, "PINNED_FIXTURE_NON_SENIOR_TEAM_CONFLICT"):
            self._resolve(match, u20_row, ctx)

        u23_round_row = copy.deepcopy(u20_row)
        u23_round_row["teams"]["home"]["name"] = "Chile"
        u23_round_row["teams"]["away"]["name"] = "Colombia"
        u23_round_row["league"]["round"] = "U23 Friendly"
        with self.assertRaisesRegex(ValueError, "PINNED_FIXTURE_CATEGORY_UNCORROBORATED: NON_SENIOR_ROUND"):
            self._resolve(match, u23_round_row, ctx)

    def test_mismatched_fixture_or_team_ids_blocked(self) -> None:
        ctx = {
            "fixture_id": 1640519,
            "kickoff": datetime.fromisoformat("2026-11-17T23:00:00+00:00"),
            "selection": {
                "home_api_id": 2383,
                "away_api_id": 8,
                "league_id": 10,
                "season": 2026,
                "gender": "M",
            },
        }
        base_row = {
            "fixture": {"id": 1640519, "date": "2026-11-17T23:00:00+00:00", "status": {"short": "NS"}},
            "league": {"id": 10, "name": "Friendlies", "country": "World", "season": 2026},
            "teams": {"home": {"id": 2383, "name": "Chile"}, "away": {"id": 8, "name": "Colombia"}},
        }
        match = self._make_match(event_id=7909714, home_name="Chile", away_name="Colombia")

        wrong_fid = copy.deepcopy(base_row)
        wrong_fid["fixture"]["id"] = 9999999
        with self.assertRaisesRegex(ValueError, "PINNED_FIXTURE_ID_CONFLICT"):
            self._resolve(match, wrong_fid, ctx)

        wrong_team = copy.deepcopy(base_row)
        wrong_team["teams"]["away"]["id"] = 999
        with self.assertRaisesRegex(ValueError, "PINNED_FIXTURE_TEAM_ORDER_CONFLICT"):
            self._resolve(match, wrong_team, ctx)

    def test_uncorroborated_or_contradictory_evidence_blocked(self) -> None:
        # selected.gender='M' + league_id=10 sin equipos sénior corroborados NO pasa.
        uncorroborated_ctx = {
            "fixture_id": 990003,
            "kickoff": datetime.fromisoformat("2026-11-17T23:00:00+00:00"),
            "selection": {
                "home_api_id": 9101,
                "away_api_id": 9102,
                "league_id": 10,
                "season": 2026,
                "gender": "M",
            },
        }
        non_curated_row = {
            "fixture": {"id": 990003, "date": "2026-11-17T23:00:00+00:00", "status": {"short": "NS"}},
            "league": {"id": 10, "name": "Friendlies", "country": "World", "season": 2026},
            "teams": {"home": {"id": 9101, "name": "Unknown Select XI"}, "away": {"id": 9102, "name": "Regional XI"}},
        }
        match = self._make_match(event_id=7990003, home_name="Unknown Select XI", away_name="Regional XI")
        with self.assertRaisesRegex(ValueError, "PINNED_FIXTURE_CATEGORY_UNCORROBORATED"):
            self._resolve(match, non_curated_row, uncorroborated_ctx)

        # Evidencia persistida contradictoria frente al proveedor debe bloquear.
        contradictory_ctx = {
            "fixture_id": 1640519,
            "kickoff": datetime.fromisoformat("2026-11-17T23:00:00+00:00"),
            "selection": {
                "home_api_id": 2383,
                "away_api_id": 8,
                "league_id": 10,
                "season": 2026,
                "gender": "M",
                "national_team_evidence": {
                    "source": "CURATED_SENIOR_NATIONAL_TEAM_CATALOG",
                    "level": "SENIOR",
                    "gender": "M",
                    "home_canonical": "argentina",
                    "away_canonical": "colombia",
                },
            },
        }
        real_row = {
            "fixture": {"id": 1640519, "date": "2026-11-17T23:00:00+00:00", "status": {"short": "NS"}},
            "league": {"id": 10, "name": "Friendlies", "country": "World", "season": 2026},
            "teams": {"home": {"id": 2383, "name": "Chile"}, "away": {"id": 8, "name": "Colombia"}},
        }
        with self.assertRaisesRegex(ValueError, "PINNED_FIXTURE_CATEGORY_CONFLICT: PERSISTED_HOME_MISMATCH"):
            self._resolve(match, real_row, contradictory_ctx)


if __name__ == "__main__":
    unittest.main()
