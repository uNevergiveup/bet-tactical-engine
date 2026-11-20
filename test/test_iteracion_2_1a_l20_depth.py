#!/usr/bin/env python3
"""Pruebas Iteración 2.1A: separación microcontexto 6 vs L20 goles, integridad temporal,
comparador legacy P-1..P-6, invariantes del estimador y preservación de snapshots históricos.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from config import constants
from bet_engine.context import ObservationStore
from bet_engine.modeling import CoherentMatchModeler
from bet_engine.modeling.distributions import MODEL_VERSION
from bet_engine.orchestration import ShadowPipelineRunner
from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt
from generate_context import (
    APITeam,
    DeepHistoryCollector,
    MicroHistoryFormatter,
)


ROOT = Path(__file__).resolve().parent.parent


class _OfflineMixedL20API:
    """Simula /fixtures?team=&last=20 (solo score) + /fixtures?ids= (detalle) con 1 post-cutoff."""

    def __init__(self, team_id: int, total_ft: int = 20, *, include_post_cutoff: bool = True) -> None:
        self.team_id = team_id
        self.total_ft = total_ft
        self.include_post_cutoff = include_post_cutoff
        self.asof = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.calls: list[tuple[str, dict]] = []

    def get(self, endpoint: str, params: dict, asof: datetime | None = None) -> dict:
        self.calls.append((endpoint, dict(params)))
        acq = (self.asof - timedelta(minutes=10)).isoformat()
        if "team" in params:
            limit = int(params.get("last", 20))
            rows = []
            if self.include_post_cutoff:
                # Partido en curso / posterior al cutoff que debe ser filtrado
                rows.append({
                    "fixture": {"id": 990000 + self.team_id, "date": (self.asof + timedelta(hours=2)).isoformat(), "status": {"short": "NS"}},
                    "league": {"id": 39, "name": "Premier League", "season": 2026, "type": "League", "country": "England"},
                    "teams": {"home": {"id": self.team_id, "name": "T"}, "away": {"id": self.team_id + 100, "name": "Opp"}},
                    "goals": {"home": None, "away": None},
                    "score": {"halftime": {"home": None, "away": None}},
                })
            for idx in range(min(limit, self.total_ft)):
                ko = self.asof - timedelta(days=3 + idx * 4)
                is_home = idx % 2 == 0
                rows.append({
                    "fixture": {"id": 500000 + self.team_id * 100 + idx, "date": ko.isoformat(), "status": {"short": "FT"}},
                    "league": {"id": 39, "name": "Premier League", "season": 2026, "type": "League", "country": "England"},
                    "teams": {
                        "home": {"id": self.team_id if is_home else self.team_id + 200 + idx, "name": "Home"},
                        "away": {"id": self.team_id + 200 + idx if is_home else self.team_id, "name": "Away"},
                    },
                    "goals": {"home": 2 if is_home else 1, "away": 1 if is_home else 1},
                    "score": {"halftime": {"home": 1, "away": 0}},
                })
            return {"_acquired_at_utc": acq, "response": rows}

        if "ids" in params:
            ids = [int(x) for x in str(params["ids"]).split("-") if x]
            rows = []
            for fid in ids:
                idx = fid % 100
                ko = self.asof - timedelta(days=3 + idx * 4)
                is_home = idx % 2 == 0
                rows.append({
                    "fixture": {"id": fid, "date": ko.isoformat(), "status": {"short": "FT"}},
                    "league": {"id": 39, "name": "Premier League", "season": 2026, "type": "League", "country": "England"},
                    "teams": {
                        "home": {"id": self.team_id if is_home else self.team_id + 200 + idx, "name": "Home"},
                        "away": {"id": self.team_id + 200 + idx if is_home else self.team_id, "name": "Away"},
                    },
                    "goals": {"home": 2 if is_home else 1, "away": 1 if is_home else 1},
                    "score": {"halftime": {"home": 1, "away": 0}},
                    "statistics": [
                        {"team": {"id": self.team_id}, "statistics": [{"type": "Corner Kicks", "value": 6}, {"type": "Total Shots", "value": 14}, {"type": "Yellow Cards", "value": 2}]},
                        {"team": {"id": self.team_id + 200 + idx}, "statistics": [{"type": "Corner Kicks", "value": 4}, {"type": "Total Shots", "value": 9}, {"type": "Yellow Cards", "value": 2}]},
                    ],
                    "events": [],
                    "lineups": [],
                })
            return {"_acquired_at_utc": acq, "response": rows}
        return {"_acquired_at_utc": acq, "response": []}


class TestIteracion21AL20Depth(unittest.TestCase):
    def test_default_limits_separate_micro_6_detail_6_and_deep_20(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PREMATCH_DEEP_HISTORY_LIMIT", None)
            os.environ.pop("PREMATCH_DEEP_HISTORY_DETAIL_LIMIT", None)
            h_lim = constants.resolve_deep_history_limit()
            d_lim = constants.resolve_deep_history_detail_limit(None, history_limit=h_lim)
            self.assertEqual(constants.MICRO_HISTORY_LIMIT, 6)
            self.assertEqual(h_lim, 20)
            self.assertEqual(d_lim, 6)

    def test_l20_score_only_tail_preserves_goals_micro_txt_and_blocks_post_cutoff(self) -> None:
        h_lim = constants.resolve_deep_history_limit()
        d_lim = constants.resolve_deep_history_detail_limit(None, history_limit=h_lim)
        api_h = _OfflineMixedL20API(10, total_ft=20, include_post_cutoff=True)
        api_a = _OfflineMixedL20API(20, total_ft=20, include_post_cutoff=True)
        team_h = APITeam(10, "Home FC", "HFC", "England", 1900, False, "MEN", None, "Arena")
        team_a = APITeam(20, "Away FC", "AFC", "England", 1900, False, "MEN", None, "Park")

        out_h = DeepHistoryCollector(api_h).collect_team_history(team_h, h_lim, asof=api_h.asof, detail_limit=d_lim)
        out_a = DeepHistoryCollector(api_a).collect_team_history(team_a, h_lim, asof=api_a.asof, detail_limit=d_lim)

        # Exactamente 2 llamadas por equipo: fixtures?team=last=20 + fixtures?ids= (6 ids)
        self.assertEqual(len(api_h.calls), 2)
        self.assertEqual(out_h["completed_fixtures_count"], 20)
        self.assertEqual(out_h["complete_stats_count"], 6)
        self.assertEqual(out_h["partial_or_score_only_count"], 14)
        self.assertFalse(any(fid >= 990000 for fid in out_h["fixture_ids"]))

        # Microcontexto cualitativo TXT permanece en P-1..P-6
        txt_lines: list[str] = []
        MicroHistoryFormatter.format_team_history(txt_lines, out_h, team_h)
        txt = "\n".join(txt_lines)
        self.assertIn("Partidos utilizados: 6", txt)
        self.assertIn("Partidos adicionales en JSON histórico: 14 (total=20)", txt)
        self.assertNotIn("P-7", txt)

        # ObservationStore + ShadowPipelineRunner reciben 20 para goles, 6 para corners/tarjetas y comparan L6/L10/L20
        payload = {
            "match": {"event_id": "test_l20_21a", "home_name": "Home FC", "away_name": "Away FC"},
            "fixture": {
                "fixture_id": 777777,
                "league_id": 39,
                "season": 2026,
                "date": (api_h.asof + timedelta(hours=3)).isoformat(),
                "home_id": 10,
                "away_id": 20,
                "home_name": "Home FC",
                "away_name": "Away FC",
            },
            "generated_at": api_h.asof.isoformat(),
            "context": {"home_history_deep": out_h, "away_history_deep": out_a},
        }
        bundle = ObservationStore.from_match_engine_json(payload)
        self.assertEqual(len(bundle.home_matches), 20)
        self.assertEqual(len(bundle.away_matches), 20)

        model = CoherentMatchModeler.build_model(bundle)
        self.assertEqual(model.model_version, MODEL_VERSION)
        self.assertEqual(model.families["GOALS_FT"].home_sample_n, 20)
        self.assertEqual(model.families["GOALS_FT"].away_sample_n, 20)
        self.assertEqual(model.families["CORNERS"].home_sample_n, 6)
        self.assertEqual(model.families["YELLOW_CARDS"].away_sample_n, 6)

        rep = ShadowPipelineRunner(min_lead_minutes=45).run_shadow_on_bundle(
            bundle,
            target_status="NS",
            odds_map={"home_win": 1.85, "draw": 3.5, "away_win": 4.2, "over_2_5_goals": 1.9},
        )
        self.assertEqual(rep.model_version, MODEL_VERSION)
        self.assertTrue(bool(rep.generated_utc))
        self.assertEqual(rep.decision, "NO_BET")
        hc = rep.historical_depth_comparison
        self.assertEqual(hc["new_home_matches_n"], 20)
        self.assertEqual(hc["new_away_matches_n"], 20)
        self.assertEqual(hc["new_home_detailed_n"], 6)
        self.assertEqual(hc["home_score_only_n"], 14)
        self.assertEqual(hc["home_goals_valid_n"], 20)
        self.assertEqual(hc["legacy_p6_home_matches_n"], 6)
        self.assertEqual(hc["extra_matches_preserved_home"], 14)
        self.assertEqual(hc["window_comparison_l6_l10_l20"]["L6"]["home_sample_n"], 6)
        self.assertEqual(hc["window_comparison_l6_l10_l20"]["L10"]["home_sample_n"], 10)
        self.assertEqual(hc["window_comparison_l6_l10_l20"]["L20"]["home_sample_n"], 20)

    def test_frozen_7909713_unchanged_by_default_depth_increase(self) -> None:
        me_path = ROOT / "outputs/matches/7909713/inputs/match_engine_7909713.json"
        odds_path = ROOT / "outputs/matches/7909713/inputs/cuotas_bwin_7909713.txt"
        if not (me_path.is_file() and odds_path.is_file()):
            self.skipTest("Snapshot 7909713 no presente")
        payload = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload, case_dir=me_path.parent.parent)
        self.assertEqual(len(bundle.home_matches), 6)
        self.assertEqual(len(bundle.away_matches), 6)
        model = CoherentMatchModeler.build_model(bundle)
        self.assertEqual(model.outcomes_1x2["home_win"], 0.6372)
        self.assertEqual(model.families["GOALS_FT"].lambda_home, 2.1916)
        self.assertEqual(model.families["GOALS_FT"].lambda_away, 1.0162)

        odds = parse_bwin_odds_txt(odds_path, home_name=bundle.home_name, away_name=bundle.away_name)
        rep = ShadowPipelineRunner(min_lead_minutes=45).run_shadow_on_bundle(
            bundle, target_status="NS", odds_map=odds, bwin_txt_path=odds_path
        )
        self.assertEqual(rep.decision, "NO_BET")
        self.assertEqual(rep.historical_depth_comparison["new_home_matches_n"], 6)
        self.assertEqual(rep.historical_depth_comparison["legacy_p6_home_matches_n"], 6)


if __name__ == "__main__":
    unittest.main()
