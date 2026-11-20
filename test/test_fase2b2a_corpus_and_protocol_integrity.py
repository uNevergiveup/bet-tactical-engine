#!/usr/bin/env python3
"""Pruebas Fase 2B.2A — Integridad de competición, tarjetas rojas PREMATCH,
consistencia de evaluadores (max_goals=10 vs 12) y trazabilidad de manifiestos.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.context.history_quality import audit_match_history_signals
from bet_engine.context.observation_store import (
    HistoricalTeamMatch,
    MatchObservationBundle,
    ObservationStore,
    build_league_catalog_from_preselection,
    same_competition_season,
    same_meaningful_stratum,
)
from bet_engine.modeling.calibration import WalkForwardEvaluator
from bet_engine.modeling.distributions import (
    CoherentMatchModeler,
    _estimate_team_rate_detailed,
    dixon_coles_joint_grid,
)
from bet_engine.modeling.prospective_registry import (
    ProspectiveRegistryError,
    load_frozen_configs,
)

ROOT = Path(__file__).resolve().parent.parent


def _mk_hm(
    *,
    fid: int,
    tid: int,
    oid: int,
    ko: datetime,
    venue: str = "HOME",
    lid: int = 39,
    lname: str = "Premier League",
    season: int = 2026,
    stratum: str = "DOMESTIC_LEAGUE",
    hg: float = 2.0,
    ag: float = 1.0,
    red_dist: bool = False,
    red_status: str = "RED_CARD_UNKNOWN",
) -> HistoricalTeamMatch:
    avail = (ko + timedelta(minutes=125)).isoformat()
    red_ver = red_status in ("RED_CARD_CONFIRMED", "NO_RED_CARD_VERIFIED")
    return HistoricalTeamMatch(
        fixture_id=fid,
        team_id=tid,
        opponent_id=oid,
        kickoff_utc=ko.isoformat(),
        available_at_utc=avail,
        league_id=lid,
        league_name=lname,
        season=season,
        competition_stratum=stratum,
        venue=venue,
        status="FT",
        coach_id=None,
        coach_name=None,
        red_card_distortion=red_dist,
        own_metrics={"goals": hg, "corners": 5.0},
        conceded_metrics={"goals": ag, "corners": 4.0},
        has_detailed_stats=True,
        source="synthetic",
        red_card_detail={
            "red_card_status": red_status,
            "red_card_verified": red_ver,
            "distortion_side": "OWN_EARLY" if red_dist else ("NONE" if red_ver else "UNKNOWN"),
        },
    )


class TestFase2B2ACorpusAndProtocolIntegrity(unittest.TestCase):
    def test_1_order_invariance_across_bundles_and_walk_forward(self) -> None:
        base_ko = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
        # Target A (liga 525 continental) y Target B (liga 139 doméstica) observan los mismos partidos 901 y 902
        shared_obs_home = [
            {
                "fixture_id": 901,
                "team_id": 10,
                "opponent_id": 20,
                "kickoff_utc": (base_ko + timedelta(days=7 * i)).isoformat(),
                "status": "FT",
                "league_id": 525 if i == 0 else 139,
                "season": 2026,
                "venue": "HOME",
                "own": {"goals": 2.0},
                "conceded": {"goals": 1.0},
            }
            for i in range(12)
        ]
        # Cambiamos fixture_id único por ronda
        for idx, row in enumerate(shared_obs_home):
            row["fixture_id"] = 900 + idx
        target_a = {
            "fixture_id": 9901,
            "league_id": 525,
            "league": "UEFA Champions League Women",
            "league_type": "Cup",
            "country": "World",
            "season": 2026,
            "kickoff_utc": (base_ko + timedelta(days=100)).isoformat(),
            "home_id": 10,
            "away_id": 20,
            "home": "Team 10",
            "away": "Team 20",
            "statistical_observations": {"home": copy.deepcopy(shared_obs_home), "away": []},
        }
        target_b = {
            "fixture_id": 9902,
            "league_id": 139,
            "league": "Serie A Women",
            "league_type": "League",
            "country": "Italy",
            "season": 2026,
            "kickoff_utc": (base_ko + timedelta(days=107)).isoformat(),
            "home_id": 10,
            "away_id": 30,
            "home": "Team 10",
            "away": "Team 30",
            "statistical_observations": {"home": copy.deepcopy(shared_obs_home), "away": []},
        }

        cat_ab = build_league_catalog_from_preselection([target_a, target_b], include_competition_registry=False)
        cat_ba = build_league_catalog_from_preselection([target_b, target_a], include_competition_registry=False)
        self.assertEqual(cat_ab, cat_ba)

        def _collect(targets, use_cat: bool):
            cat = build_league_catalog_from_preselection(targets, include_competition_registry=False) if use_cat else None
            seen = {}
            for t in targets:
                b = ObservationStore.from_preselection_match(t, cutoff_utc=t["kickoff_utc"], league_catalog=cat)
                for m in b.home_matches:
                    prev = seen.get(m.fixture_id)
                    seen[m.fixture_id] = (
                        ObservationStore.prefer_informative_historical_match(prev, m)
                        if prev is not None
                        else m
                    )
            return {fid: (m.league_name, m.competition_stratum) for fid, m in sorted(seen.items())}

        self.assertEqual(_collect([target_a, target_b], False), _collect([target_b, target_a], False))
        self.assertEqual(_collect([target_a, target_b], True), _collect([target_b, target_a], True))
        merged = _collect([target_a, target_b], True)
        self.assertEqual(merged[900], ("UEFA Champions League Women", "CONTINENTAL_CLUB"))
        self.assertEqual(merged[901], ("Serie A Women", "DOMESTIC_LEAGUE"))

    def test_2_missing_ids_and_unknown_never_equated(self) -> None:
        ko = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
        m_missing = _mk_hm(
            fid=1, tid=10, oid=20, ko=ko, lid=0, lname="", season=0, stratum="UNKNOWN"
        )
        m_real = _mk_hm(
            fid=2, tid=10, oid=20, ko=ko, lid=39, lname="Premier League", season=2026, stratum="DOMESTIC_LEAGUE"
        )
        # Datos ausentes (0, 0) y UNKNOWN nunca cuentan como coincidencia real
        self.assertFalse(same_competition_season(m_missing, 0, 0))
        self.assertFalse(same_meaningful_stratum(m_missing, "UNKNOWN"))
        self.assertTrue(same_competition_season(m_real, 39, 2026))
        self.assertTrue(same_meaningful_stratum(m_real, "DOMESTIC_LEAGUE"))

        sig = audit_match_history_signals(
            m_missing,
            target_ko=ko + timedelta(days=7),
            target_league_id=0,
            target_season=0,
            target_venue="HOME",
            target_stratum="UNKNOWN",
        )
        self.assertFalse(sig["same_competition_season"])
        self.assertFalse(sig["same_stratum"])

        # En from_preselection_match, target sin league_id (0) no contagia target["league"] a filas sin league_id (0)
        bundle = ObservationStore.from_preselection_match(
            {
                "fixture_id": 777,
                "league_id": 0,
                "league": "Premier League",
                "season": 0,
                "kickoff_utc": (ko + timedelta(days=7)).isoformat(),
                "home_id": 10,
                "away_id": 20,
                "statistical_observations": {
                    "home": [
                        {
                            "fixture_id": 500,
                            "team_id": 10,
                            "opponent_id": 20,
                            "kickoff_utc": ko.isoformat(),
                            "status": "FT",
                            "league_id": 0,
                            "season": 0,
                            "venue": "HOME",
                            "own": {"goals": 1},
                            "conceded": {"goals": 0},
                        }
                    ],
                    "away": [],
                },
            },
            cutoff_utc=ko + timedelta(days=7),
        )
        self.assertEqual(bundle.home_matches[0].league_name, "")
        self.assertEqual(bundle.home_matches[0].competition_stratum, "UNKNOWN")

    def test_3_unknown_weight_fix_isolated_without_mutating_frozen_baseline(self) -> None:
        ko0 = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
        # 5 partidos en una liga UNKNOWN con muchos goles (4.0) y 5 en otra liga conocida (1.0)
        hist = tuple(
            _mk_hm(
                fid=100 + i,
                tid=1,
                oid=50 + i,
                ko=ko0 + timedelta(days=7 * i),
                lid=999 if i < 5 else 39,
                lname="" if i < 5 else "Premier League",
                stratum="UNKNOWN" if i < 5 else "DOMESTIC_LEAGUE",
                hg=4.0 if i < 5 else 0.0,
                ag=1.0,
            )
            for i in range(10)
        )
        # Baseline congelado (default strict_competition_identity=False) sigue equiparando UNKNOWN==UNKNOWN con 0.50
        base_est = _estimate_team_rate_detailed(
            hist,
            hist,
            metric="goals",
            target_league_id=888,
            target_season=2026,
            target_stratum="UNKNOWN",
            own_venue="HOME",
            opp_venue="AWAY",
            prior_mean=1.56,
        )
        # Variante aislada (strict_competition_identity=True) trata UNKNOWN como peso 0.25 igual que otra liga distinta
        var_est = _estimate_team_rate_detailed(
            hist,
            hist,
            metric="goals",
            target_league_id=888,
            target_season=2026,
            target_stratum="UNKNOWN",
            own_venue="HOME",
            opp_venue="AWAY",
            prior_mean=1.56,
            strict_competition_identity=True,
        )
        self.assertGreater(base_est["lambda"], var_est["lambda"])
        # En CoherentMatchModeler default sigue inalterado respecto a baseline
        b_dummy = MatchObservationBundle(
            event_id="ev",
            fixture_id=999,
            league_id=888,
            season=2026,
            kickoff_utc=(ko0 + timedelta(days=80)).isoformat(),
            cutoff_utc=(ko0 + timedelta(days=80)).isoformat(),
            home_id=1,
            away_id=2,
            home_name="A",
            away_name="B",
            home_matches=hist,
            away_matches=hist,
            facts=(),
            contextual_evidence=(),
            rejected_records=(),
            windows={},
            source_sha256="0" * 64,
        )
        m_default = CoherentMatchModeler.build_model(b_dummy)
        m_strict = CoherentMatchModeler.build_model(b_dummy, strict_competition_identity=True)
        # CoherentMatchModeler ya resuelve target_stratum=None cuando todo es UNKNOWN; comprobar misma interfaz segura
        self.assertEqual(m_default.families["GOALS_FT"].lambda_home, m_strict.families["GOALS_FT"].lambda_home)

    def test_4_5_prematch_principal_does_not_filter_target_red_card_and_distinguishes_unknown(self) -> None:
        base_ko = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
        # Construir serie de 8 rondas; en la ronda final (target) ambos equipos sufren roja confirmada
        h_list = []
        a_list = []
        for idx in range(12):
            ko = base_ko + timedelta(days=idx * 7)
            is_last = idx == 11
            hm = _mk_hm(
                fid=2000 + idx,
                tid=1,
                oid=2,
                ko=ko,
                venue="HOME",
                hg=3.0 if is_last else 2.0,
                ag=0.0,
                red_dist=is_last,
                red_status="RED_CARD_CONFIRMED" if is_last else ("NO_RED_CARD_VERIFIED" if idx % 2 == 0 else "RED_CARD_UNKNOWN"),
            )
            am = _mk_hm(
                fid=2000 + idx,
                tid=2,
                oid=1,
                ko=ko,
                venue="AWAY",
                hg=0.0,
                ag=3.0 if is_last else 2.0,
                red_dist=is_last,
                red_status="RED_CARD_CONFIRMED" if is_last else ("NO_RED_CARD_VERIFIED" if idx % 2 == 0 else "RED_CARD_UNKNOWN"),
            )
            h_list.append(hm)
            a_list.append(am)

        # Verificar semántica de 3 estados en HistoricalTeamMatch
        self.assertEqual(h_list[0].red_card_status, "NO_RED_CARD_VERIFIED")
        self.assertTrue(h_list[0].red_card_verified)
        self.assertEqual(h_list[1].red_card_status, "RED_CARD_UNKNOWN")
        self.assertFalse(h_list[1].red_card_verified)
        self.assertEqual(h_list[-1].red_card_status, "RED_CARD_CONFIRMED")
        self.assertTrue(h_list[-1].red_card_verified)

        b = MatchObservationBundle(
            event_id="ev_rc",
            fixture_id=9999,
            league_id=39,
            season=2026,
            kickoff_utc=(base_ko + timedelta(days=100)).isoformat(),
            cutoff_utc=(base_ko + timedelta(days=100)).isoformat(),
            home_id=1,
            away_id=2,
            home_name="H",
            away_name="A",
            home_matches=tuple(h_list),
            away_matches=tuple(a_list),
            facts=(),
            contextual_evidence=(),
            rejected_records=(),
            windows={},
            source_sha256="0" * 64,
        )

        # Principal PREMATCH (default exclude_target_red_cards=False) NO excluye el partido final con roja objetivo
        jf_pre = WalkForwardEvaluator.evaluate_joint_match_walk_forward([b], min_prior_per_team=6)
        jf_sec = WalkForwardEvaluator.evaluate_joint_match_walk_forward([b], min_prior_per_team=6, exclude_target_red_cards=True)
        self.assertEqual(jf_pre["home_win"]["n"], 6)
        self.assertEqual(jf_sec["home_win"]["n"], 5)
        self.assertEqual(jf_pre["protocol_audit"]["target_red_card_excluded_n"], 0)
        self.assertEqual(jf_sec["protocol_audit"]["target_red_card_excluded_n"], 1)
        self.assertEqual(jf_pre["protocol_audit"]["max_goals"], 12)

        wd_pre = WalkForwardEvaluator.evaluate_window_depth_and_weighting([b], windows=(6,), min_prior_per_team=6, bootstrap_samples=50)
        wd_sec = WalkForwardEvaluator.evaluate_window_depth_and_weighting([b], windows=(6,), min_prior_per_team=6, bootstrap_samples=50, exclude_target_red_cards=True)
        self.assertEqual(wd_pre["evaluated_targets_n"], 6)
        self.assertEqual(wd_sec["evaluated_targets_n"], 5)

        sm_pre = WalkForwardEvaluator.evaluate_single_team_metric_windows([b], metric="corners", line=4.5, windows=(6,), min_prior_per_team=6, bootstrap_samples=50)
        sm_sec = WalkForwardEvaluator.evaluate_single_team_metric_windows([b], metric="corners", line=4.5, windows=(6,), min_prior_per_team=6, bootstrap_samples=50, exclude_target_red_cards=True)
        self.assertEqual(sm_pre["out_of_sample_n"], 12)
        self.assertEqual(sm_sec["out_of_sample_n"], 10)

    def test_6_max_goals_10_vs_12_numerical_difference_and_frozen_baseline_artifacts(self) -> None:
        g10 = dixon_coles_joint_grid(3.2, 2.8, rho=-0.04, max_goals=10)
        g12 = dixon_coles_joint_grid(3.2, 2.8, rho=-0.04, max_goals=12)
        hw10 = sum(g10[i][j] for i in range(11) for j in range(11) if i > j)
        hw12 = sum(g12[i][j] for i in range(13) for j in range(13) if i > j)
        self.assertNotAlmostEqual(hw10, hw12, places=7)

        comp_path = ROOT / "audit/fase2b_backtesting/PROTOCOL_2B2A_COMPARISON.json"
        if comp_path.is_file():
            comp = json.loads(comp_path.read_text(encoding="utf-8"))
            self.assertEqual(comp["frozen_baseline_2_1a_secondary_4489"]["train_n"], 2170)
            self.assertEqual(comp["frozen_baseline_2_1a_secondary_4489"]["test_n"], 2319)
            self.assertEqual(comp["frozen_baseline_2_1a_secondary_4489"]["test_L20_ALL"]["1x2_multiclass"]["rps"], 0.21823)
            self.assertEqual(comp["protocol_2b2a_principal_prematch_4496"]["train_n"], 2173)
            self.assertEqual(comp["protocol_2b2a_principal_prematch_4496"]["test_n"], 2323)
            self.assertEqual(comp["corpus_integrity"]["clean_order_invariant"]["stratum_overwrites_prevented_n"], 440)

    def test_7_manifests_traceability_and_historical_preservation(self) -> None:
        legacy_p = ROOT / "audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json"
        v2_p = ROOT / "audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json"
        self.assertTrue(legacy_p.is_file())
        self.assertTrue(v2_p.is_file())

        # Histórico 2.2B permanece sin alteración y sigue detectando cambio de hashes en código nuevo
        with self.assertRaises(ProspectiveRegistryError) as ctx:
            load_frozen_configs(legacy_p, verify_code_hashes=True)
        self.assertIn("MANIFEST_CODE_HASH_MISMATCH", str(ctx.exception))

        # Manifiesto V2 valida íntegramente contra los archivos reales y referencia al SHA-256 original de 2.2B
        cfgs, ver = load_frozen_configs(v2_p, verify_code_hashes=True)
        self.assertEqual(ver, "2B.2A-frozen-models")
        self.assertIn("M0", cfgs)
        v2_doc = json.loads(v2_p.read_text(encoding="utf-8"))
        self.assertEqual(v2_doc["supersedes_manifest_sha256"], hashlib.sha256(legacy_p.read_bytes()).hexdigest())

    def test_8_closed_prematch_expedients_unmodified_in_git(self) -> None:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", "outputs/matches/"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        # Solo verificamos que no haya modificaciones tracked introducidas en outputs/matches
        changed_tracked = [
            line for line in proc.stdout.splitlines() if line and not line.startswith("?? ")
        ]
        self.assertEqual(changed_tracked, [])


if __name__ == "__main__":
    unittest.main()
