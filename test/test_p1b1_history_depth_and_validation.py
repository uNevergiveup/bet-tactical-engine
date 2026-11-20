"""Pruebas P1-B1: profundidad histórica configurable (6/10/20), calidad muestral,
auditoría point-in-time sin fuga temporal y evaluación Walk-Forward pareada.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config.constants as constants
from bet_engine.context import (
    ObservationStore,
    audit_historical_window_quality,
    build_point_in_time_opponent_lookup,
)
from bet_engine.context.observation_store import HistoricalTeamMatch
from bet_engine.modeling import CoherentMatchModeler, WalkForwardEvaluator
from generate_context import (
    APITeam,
    DeepHistoryCollector,
    MicroHistoryFormatter,
)

ROOT = Path(__file__).resolve().parent.parent


class _FakeOfflineAPI:
    """Stub estricto de solo memoria para probar DeepHistoryCollector sin red."""

    def __init__(self, team_id: int, total_fixtures: int, *, duplicate_first: bool = False) -> None:
        self.team_id = team_id
        self.total_fixtures = total_fixtures
        self.duplicate_first = duplicate_first
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.asof = datetime(2026, 11, 17, 19, 0, tzinfo=timezone.utc)
        base_ko = datetime(2026, 11, 10, 19, 0, tzinfo=timezone.utc)
        self._items: list[dict[str, object]] = []
        for idx in range(total_fixtures):
            fid = 900000 - idx
            ko = (base_ko - timedelta(days=idx * 10)).isoformat()
            self._items.append(
                {
                    "fixture": {"id": fid, "date": ko, "status": {"short": "FT"}},
                    "league": {"id": 39, "name": "Premier League", "season": 2026, "type": "League", "country": "England"},
                    "teams": {
                        "home": {"id": team_id if idx % 2 == 0 else (200 + idx), "name": "TeamH"},
                        "away": {"id": (200 + idx) if idx % 2 == 0 else team_id, "name": "TeamA"},
                    },
                    "goals": {"home": 2 if idx % 2 == 0 else 1, "away": 1},
                    "score": {"halftime": {"home": 1, "away": 0}},
                    "statistics": [
                        {
                            "team": {"id": team_id},
                            "statistics": [
                                {"type": "Corner Kicks", "value": 5},
                                {"type": "Total Shots", "value": 12},
                                {"type": "Yellow Cards", "value": 2},
                            ],
                        },
                        {
                            "team": {"id": 200 + idx},
                            "statistics": [
                                {"type": "Corner Kicks", "value": 4},
                                {"type": "Total Shots", "value": 8},
                                {"type": "Yellow Cards", "value": 1},
                            ],
                        },
                    ],
                    "lineups": [
                        {"team": {"id": team_id}, "coach": {"id": 501, "name": "Coach Alpha"}, "startXI": []},
                        {"team": {"id": 200 + idx}, "coach": {"id": 600 + idx, "name": f"Coach {idx}"}, "startXI": []},
                    ],
                    "events": [],
                }
            )

    def get(self, endpoint: str, params: dict[str, object], asof: datetime | None = None) -> dict[str, object]:
        self.calls.append((endpoint, dict(params)))
        acq = "2026-11-17T18:00:00+00:00"
        if "team" in params:
            limit = int(params["last"])
            resp = [
                {
                    "fixture": it["fixture"],
                    "league": it["league"],
                    "teams": it["teams"],
                    "goals": it["goals"],
                    "score": it["score"],
                }
                for it in self._items[:limit]
            ]
            if self.duplicate_first and resp:
                resp.append(resp[0])
            return {"response": resp, "_acquired_at_utc": acq}
        ids = [int(x) for x in str(params.get("ids", "")).split("-") if x]
        id_set = set(ids)
        resp = [it for it in self._items if int(it["fixture"]["id"]) in id_set]
        return {"response": resp, "_acquired_at_utc": acq}


def _make_hist_match(
    *,
    fid: int,
    tid: int,
    opp_id: int,
    ko: datetime,
    venue: str,
    own_goals: int,
    opp_goals: int,
    corners: int | None = 5,
    yellow: int | None = 2,
    league_id: int = 39,
    season: int = 2026,
    stratum: str = "DOMESTIC_LEAGUE",
    coach_id: int | None = 11,
    coach_name: str | None = "Coach Main",
    red_distortion: bool = False,
) -> HistoricalTeamMatch:
    avail = (ko + timedelta(minutes=125)).isoformat()
    has_det = corners is not None
    return HistoricalTeamMatch(
        fixture_id=fid,
        team_id=tid,
        opponent_id=opp_id,
        kickoff_utc=ko.isoformat(),
        available_at_utc=avail,
        league_id=league_id,
        league_name="Premier League" if stratum == "DOMESTIC_LEAGUE" else "Friendlies",
        season=season,
        competition_stratum=stratum,
        venue=venue,
        status="FT",
        coach_id=coach_id,
        coach_name=coach_name,
        red_card_distortion=red_distortion,
        own_metrics={
            "goals": float(own_goals),
            "goals_h1": float(min(own_goals, 1)),
            "goals_h2": float(max(0, own_goals - 1)),
            "corners": float(corners) if corners is not None else None,
            "shots": 11.0 if has_det else None,
            "on_target": 4.0 if has_det else None,
            "blocked_shots": 2.0 if has_det else None,
            "fouls": 11.0 if has_det else None,
            "yellow": float(yellow) if yellow is not None else None,
            "red": 0.0 if has_det else None,
            "offsides": 1.0 if has_det else None,
            "xg": None,
            "possession": 52.0 if has_det else None,
        },
        conceded_metrics={
            "goals": float(opp_goals),
            "goals_h1": float(min(opp_goals, 1)),
            "goals_h2": float(max(0, opp_goals - 1)),
            "corners": 4.0 if has_det else None,
            "shots": 9.0 if has_det else None,
            "on_target": 3.0 if has_det else None,
            "blocked_shots": 2.0 if has_det else None,
            "fouls": 10.0 if has_det else None,
            "yellow": 2.0 if yellow is not None else None,
            "red": 0.0 if has_det else None,
            "offsides": 1.0 if has_det else None,
            "xg": None,
            "possession": 48.0 if has_det else None,
        },
        has_detailed_stats=has_det,
        source="synthetic",
        red_card_detail={"distortion_side": "OWN_EARLY" if red_distortion else "NONE"},
    )


class TestP1B1HistoryDepthAndValidation(unittest.TestCase):
    def test_config_history_limit_resolution_separates_micro_6_and_deep_20(self) -> None:
        self.assertEqual(constants.MICRO_HISTORY_LIMIT, 6)
        self.assertEqual(constants.DEEP_HISTORY_LIMIT, 20)
        self.assertEqual(constants.DEEP_HISTORY_DETAIL_LIMIT, 6)
        self.assertEqual(constants.VALID_DEEP_HISTORY_WINDOWS, (6, 10, 20))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PREMATCH_DEEP_HISTORY_LIMIT", None)
            os.environ.pop("PREMATCH_DEEP_HISTORY_DETAIL_LIMIT", None)
            self.assertEqual(constants.resolve_deep_history_limit(), 20)
            self.assertEqual(constants.resolve_deep_history_limit(6), 6)
            self.assertEqual(constants.resolve_deep_history_limit(10), 10)
            self.assertEqual(constants.resolve_deep_history_limit(20), 20)
            self.assertEqual(constants.resolve_deep_history_detail_limit(None, history_limit=20), 6)
            self.assertIsNone(constants.resolve_deep_history_detail_limit(None, history_limit=20, default=None))
            self.assertEqual(constants.resolve_deep_history_detail_limit(6, history_limit=20), 6)
            self.assertEqual(constants.resolve_deep_history_detail_limit(50, history_limit=10), 10)
        with mock.patch.dict(os.environ, {"PREMATCH_DEEP_HISTORY_LIMIT": "10"}):
            self.assertEqual(constants.resolve_deep_history_limit(), 10)
        with mock.patch.dict(os.environ, {"PREMATCH_DEEP_HISTORY_DETAIL_LIMIT": "20"}):
            self.assertEqual(constants.resolve_deep_history_detail_limit(None, history_limit=20), 20)
        with mock.patch.dict(os.environ, {"PREMATCH_DEEP_HISTORY_LIMIT": "99"}):
            with self.assertRaises(ValueError):
                constants.resolve_deep_history_limit()
        with self.assertRaises(ValueError):
            constants.resolve_deep_history_limit(2)
        with self.assertRaises(ValueError):
            constants.resolve_deep_history_limit(25)

    def test_deep_history_collector_batch_and_legacy_txt_cutoff(self) -> None:
        team = APITeam(
            id=10,
            name="Test FC",
            code="TFC",
            country="England",
            founded=1900,
            national=False,
            gender="MEN",
            logo=None,
            venue_name="Test Park",
        )

        # 20 partidos reales con 20 detalles completos en un solo batch /fixtures?ids=...
        api_20 = _FakeOfflineAPI(10, 20, duplicate_first=True)
        collector_20 = DeepHistoryCollector(api_20)
        out_20 = collector_20.collect_team_history(team, 20, asof=api_20.asof)
        self.assertEqual(out_20["requested_limit"], 20)
        self.assertEqual(out_20["completed_fixtures_count"], 20)
        self.assertEqual(out_20["complete_stats_count"], 20)
        self.assertEqual(out_20["partial_or_score_only_count"], 0)
        self.assertEqual(out_20["batches"], 1)
        self.assertEqual(len(out_20["fixture_ids"]), 20)

        # 20 solicitados pero detail_limit=6 -> preserva 14 SCORE_ONLY_NO_STATS sin inventar stats
        api_mixed = _FakeOfflineAPI(10, 20)
        out_mixed = DeepHistoryCollector(api_mixed).collect_team_history(
            team, 20, asof=api_mixed.asof, detail_limit=6
        )
        self.assertEqual(out_mixed["completed_fixtures_count"], 20)
        self.assertEqual(out_mixed["complete_stats_count"], 6)
        self.assertEqual(out_mixed["partial_or_score_only_count"], 14)
        self.assertEqual(out_mixed["detailed_fixtures"][0]["_history_detail_tier"], "COMPLETE_DETAILED")
        self.assertEqual(out_mixed["detailed_fixtures"][-1]["_history_detail_tier"], "SCORE_ONLY_NO_STATS")

        # TXT Legacy sigue intacto con exactamente 6 partidos (P-1..P-6)
        txt_lines: list[str] = []
        MicroHistoryFormatter.format_team_history(txt_lines, out_20, team)
        txt = "\n".join(txt_lines)
        self.assertIn("Partidos utilizados: 6", txt)
        self.assertIn("Partidos adicionales en JSON histórico: 14 (total=20)", txt)
        self.assertIn("P-6", txt)
        self.assertNotIn("P-7", txt)

        # Si el proveedor solo tiene 6 fixtures aunque se pidan 20, jamás fabrica partidos
        api_short = _FakeOfflineAPI(10, 6)
        out_short = DeepHistoryCollector(api_short).collect_team_history(team, 20, asof=api_short.asof)
        self.assertEqual(out_short["completed_fixtures_count"], 6)
        self.assertEqual(len(out_short["detailed_fixtures"]), 6)

    def test_observation_store_configurable_windows_and_completeness_tracking(self) -> None:
        api = _FakeOfflineAPI(10, 20)
        team_h = APITeam(10, "Home FC", "HFC", "England", 1900, False, "MEN", None, "Arena")
        out_mixed = DeepHistoryCollector(api).collect_team_history(
            team_h, 20, asof=api.asof, detail_limit=6
        )
        api_away = _FakeOfflineAPI(20, 20)
        team_a = APITeam(20, "Away FC", "AFC", "England", 1900, False, "MEN", None, "Park")
        out_away = DeepHistoryCollector(api_away).collect_team_history(
            team_a, 20, asof=api_away.asof, detail_limit=10
        )

        payload = {
            "match": {"event_id": "999999", "home_name": "Home FC", "away_name": "Away FC"},
            "fixture": {
                "fixture_id": 888888,
                "league_id": 39,
                "season": 2026,
                "date": "2026-11-17T20:00:00+00:00",
                "home_id": 10,
                "away_id": 20,
                "home_name": "Home FC",
                "away_name": "Away FC",
            },
            "generated_at": "2026-11-17T19:00:00+00:00",
            "context": {
                "home_history_deep": out_mixed,
                "away_history_deep": out_away,
            },
        }

        b20 = ObservationStore.from_match_engine_json(payload, history_limit=20)
        b10 = ObservationStore.from_match_engine_json(payload, history_limit=10)
        b6 = ObservationStore.from_match_engine_json(payload, history_limit=6)

        self.assertEqual(len(b20.home_matches), 20)
        self.assertEqual(len(b10.home_matches), 10)
        self.assertEqual(len(b6.home_matches), 6)

        l20_h = b20.windows["home"]["L20_all_competitions"]
        l6_h = b20.windows["home"]["L6_all_competitions"]
        self.assertEqual(l20_h.total_matches_available, 20)
        self.assertEqual(l20_h.detailed_matches_valid, 6)
        self.assertEqual(l20_h.partial_or_score_only_count, 14)
        self.assertEqual(l20_h.metrics["own.goals"]["valid_n"], 20)
        self.assertEqual(l20_h.metrics["own.corners"]["valid_n"], 6)
        self.assertEqual(l6_h.total_matches_available, 6)
        self.assertEqual(l6_h.detailed_matches_valid, 6)

        model_default = CoherentMatchModeler.build_model(b20)
        model_20 = CoherentMatchModeler.build_model(b20, history_limit=20)
        model_6 = CoherentMatchModeler.build_model(b20, history_limit=6)
        self.assertEqual(
            model_default.families["GOALS_FT"].lambda_home,
            model_20.families["GOALS_FT"].lambda_home,
        )
        self.assertEqual(model_6.families["GOALS_FT"].home_sample_n, 6)
        self.assertEqual(model_20.families["GOALS_FT"].home_sample_n, 20)

    def test_point_in_time_quality_audit_and_walk_forward_paired_windows(self) -> None:
        base_ko = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
        bundles = []
        synthetic_pairs: list[tuple[int, int, int, int, int]] = []
        # 24 partidos cronológicos para parejas de 4 equipos -> >= 10 partidos previos por equipo
        teams = [101, 102, 103, 104]
        matches_by_team: dict[int, list[HistoricalTeamMatch]] = {t: [] for t in teams}
        for round_idx in range(14):
            ko = base_ko + timedelta(days=round_idx * 7)
            for p_idx, (hid, aid) in enumerate(((101, 102), (103, 104)) if round_idx % 2 == 0 else ((101, 103), (102, 104))):
                fid = 50000 + round_idx * 10 + p_idx
                hg = 2 if hid in (101, 103) else 1
                ag = 1 if aid in (102, 104) else 2
                hm = _make_hist_match(
                    fid=fid,
                    tid=hid,
                    opp_id=aid,
                    ko=ko,
                    venue="HOME",
                    own_goals=hg,
                    opp_goals=ag,
                    corners=5 if round_idx >= 2 else None,
                    coach_id=10 if hid == 101 and round_idx >= 8 else 1,
                    coach_name="NewCoach" if hid == 101 and round_idx >= 8 else "OldCoach",
                )
                am = _make_hist_match(
                    fid=fid,
                    tid=aid,
                    opp_id=hid,
                    ko=ko,
                    venue="AWAY",
                    own_goals=ag,
                    opp_goals=hg,
                    corners=4 if round_idx >= 2 else None,
                    coach_id=2,
                    coach_name="CoachAway",
                )
                matches_by_team[hid].append(hm)
                matches_by_team[aid].append(am)
                synthetic_pairs.append((fid, hid, aid, hg, ag))

        # Opponent lookup as-of nunca usa partidos futuros
        opp_fn = build_point_in_time_opponent_lookup(matches_by_team, min_matches=4)
        self.assertIsNone(opp_fn(102, base_ko + timedelta(days=7)))
        opp_late = opp_fn(102, base_ko + timedelta(days=12 * 7))
        self.assertIsNotNone(opp_late)
        self.assertGreaterEqual(int(opp_late["n_prior"]), 4)

        target_meta = {
            "fixture_id": 60000,
            "league_id": 39,
            "season": 2026,
            "kickoff_utc": (base_ko + timedelta(days=15 * 7)).isoformat(),
            "home_id": 101,
            "away_id": 102,
            "home": "T101",
            "away": "T102",
            "statistical_observations": {
                "home": [
                    {
                        "fixture_id": m.fixture_id,
                        "team_id": m.team_id,
                        "opponent_id": m.opponent_id,
                        "kickoff_utc": m.kickoff_utc,
                        "status": "FT",
                        "league_id": m.league_id,
                        "season": m.season,
                        "venue": m.venue,
                        "coach_id": m.coach_id,
                        "coach_name": m.coach_name,
                        "own": m.own_metrics,
                        "conceded": m.conceded_metrics,
                    }
                    for m in reversed(matches_by_team[101])
                ],
                "away": [
                    {
                        "fixture_id": m.fixture_id,
                        "team_id": m.team_id,
                        "opponent_id": m.opponent_id,
                        "kickoff_utc": m.kickoff_utc,
                        "status": "FT",
                        "league_id": m.league_id,
                        "season": m.season,
                        "venue": m.venue,
                        "coach_id": m.coach_id,
                        "coach_name": m.coach_name,
                        "own": m.own_metrics,
                        "conceded": m.conceded_metrics,
                    }
                    for m in reversed(matches_by_team[102])
                ],
            },
        }
        bundle = ObservationStore.from_preselection_match(
            target_meta, cutoff_utc=base_ko + timedelta(days=15 * 7), history_limit=14
        )
        bundles.append(bundle)

        qual = audit_historical_window_quality(
            bundle,
            windows=(6, 10, 20),
            coach_overrides={"home": (10, "NewCoach"), "away": (2, "CoachAway")},
            opponent_strength_fn=opp_fn,
        )
        self.assertEqual(qual["sides"]["home"]["windows"]["L6"]["same_coach_n"], 6)
        self.assertEqual(qual["sides"]["home"]["windows"]["L10"]["different_coach_n"], 4)
        self.assertEqual(qual["sides"]["home"]["windows"]["L20"]["n_available"], 14)
        self.assertEqual(qual["sides"]["home"]["windows"]["L20"]["shortfall"], 6)

        wf_joint = WalkForwardEvaluator.evaluate_window_depth_and_weighting(
            bundles,
            windows=(6, 10),
            min_prior_per_team=10,
            bootstrap_samples=100,
        )
        self.assertEqual(wf_joint["temporal_leakage_violations"], 0)
        self.assertEqual(wf_joint["evaluated_targets_n"], 2)
        self.assertIn("prod_w6", wf_joint["variants"])
        self.assertIn("prod_w10", wf_joint["variants"])
        self.assertIn("w10_p1b2_mult_sos", wf_joint["variants"])
        self.assertEqual(
            wf_joint["variants"]["prod_w10"]["markets"]["over_2_5"]["n"],
            wf_joint["variants"]["prod_w6"]["markets"]["over_2_5"]["n"],
        )

        wf_corners = WalkForwardEvaluator.evaluate_single_team_metric_windows(
            bundles,
            metric="corners",
            line=4.5,
            windows=(6, 10),
            min_prior_per_team=10,
            bootstrap_samples=100,
        )
        self.assertEqual(wf_corners["sample_status"], "INSUFFICIENT_SAMPLE_LT_25")
        self.assertEqual(wf_corners["out_of_sample_n"], 4)
        self.assertEqual(wf_corners["temporal_leakage_violations"], 0)
        wf_corners_20 = WalkForwardEvaluator.evaluate_single_team_metric_windows(
            bundles,
            metric="corners",
            line=4.5,
            windows=(6, 10, 20),
            min_prior_per_team=20,
            bootstrap_samples=100,
        )
        self.assertEqual(wf_corners_20["sample_status"], "NO_OBSERVATIONS")
        self.assertEqual(wf_corners_20["out_of_sample_n"], 0)

    def test_frozen_7905487_preselection_vs_context_audit_read_only(self) -> None:
        me_path = ROOT / "outputs" / "matches" / "7905487" / "inputs" / "match_engine_7905487.json"
        pre_path = ROOT / "outputs" / "discovery" / "2026-11-17_1500_0300" / "prospective_trial" / "enriched" / "preselection.json"
        if not me_path.exists() or not pre_path.exists():
            self.skipTest("Snapshots congelados de 7905487 no presentes")

        me_payload = json.loads(me_path.read_text(encoding="utf-8"))
        b_me = ObservationStore.from_match_engine_json(me_payload, case_dir=me_path.parent.parent)
        q_me = audit_historical_window_quality(b_me, windows=(6, 10, 20))
        self.assertEqual(q_me["sides"]["home"]["total_available"], 6)
        self.assertEqual(q_me["sides"]["away"]["total_available"], 6)

        pre_payload = json.loads(pre_path.read_text(encoding="utf-8"))
        matches = pre_payload.get("matches") if isinstance(pre_payload, dict) else pre_payload
        target = next((m for m in matches if m.get("fixture_id") == 1528992), None)
        self.assertIsNotNone(target)
        b_pre = ObservationStore.from_preselection_match(target, cutoff_utc=b_me.cutoff_utc, history_limit=20)
        q_pre = audit_historical_window_quality(b_pre, windows=(6, 10, 20))
        self.assertEqual(q_pre["sides"]["home"]["total_available"], 16)
        self.assertEqual(q_pre["sides"]["away"]["total_available"], 20)
        self.assertEqual(q_pre["sides"]["home"]["windows"]["L6"]["complete_detailed_n"], 5)
        self.assertEqual(q_pre["sides"]["home"]["windows"]["L20"]["complete_detailed_n"], 5)
        self.assertEqual(q_pre["sides"]["away"]["windows"]["L20"]["complete_detailed_n"], 5)


if __name__ == "__main__":
    unittest.main()
