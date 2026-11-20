from __future__ import annotations

import unittest

from bet_engine.context.observation_store import HistoricalTeamMatch
from bet_engine.modeling.distributions import (
    DEFAULT_PRIORS,
    MODEL_VERSION,
    _estimate_team_rate_detailed,
    _resolve_prior,
)
from bet_engine.modeling.experimental_v2_1b import AsOfEloTracker
from tools.run_fase2b3a_experiments import VariantSpec, _estimate_side_rate_variant
from tools.run_fase2b3b_experiments import _murphy_brier_decomp


def _hm(
    fid: int,
    tid: int,
    opp_id: int,
    ko: str,
    av: str,
    *,
    venue: str = "HOME",
    league_id: int = 39,
    season: int = 2026,
    stratum: str = "DOMESTIC_LEAGUE",
    gf: float = 2.0,
    ga: float = 1.0,
) -> HistoricalTeamMatch:
    return HistoricalTeamMatch(
        fixture_id=fid,
        team_id=tid,
        opponent_id=opp_id,
        kickoff_utc=ko,
        available_at_utc=av,
        league_id=league_id,
        league_name="Test League",
        season=season,
        competition_stratum=stratum,
        venue=venue,
        status="FT",
        coach_id=None,
        coach_name=None,
        red_card_distortion=False,
        own_metrics={"goals": gf},
        conceded_metrics={"goals": ga},
        has_detailed_stats=True,
        source="TEST",
    )


class TestFase2B3BMechanismsAndSpec(unittest.TestCase):
    def test_production_baseline_remains_untouched(self) -> None:
        self.assertEqual(DEFAULT_PRIORS["goals"], (1.56, 1.26))
        self.assertEqual(MODEL_VERSION, "CoherentMatchModeler_baseline_2.0_2.1A")

    def test_kish_neff_exceeds_sumw_on_out_of_competition_uniform_weights(self) -> None:
        # 8 partidos en otro torneo/estrato (comp_weight = 0.25 cada uno):
        # sum(w) = 2.0, pero Kish n_eff = 2.0^2 / (8 * 0.25^2) = 8.0
        # Al evitar la doble penalización por peso < 1, la muestra pesa más que en sum(w).
        away_comp_other = tuple(
            _hm(
                i, 1, 10 + i, f"2026-08-{10+i:02d}T15:00:00+00:00", f"2026-08-{10+i:02d}T17:05:00+00:00",
                venue="HOME", league_id=999, season=2025, stratum="DOMESTIC_CUP", gf=3.0, ga=0.0,
            )
            for i in range(1, 9)
        )
        opp = tuple(
            _hm(
                100 + i, 2, 20 + i, f"2026-08-{10+i:02d}T15:00:00+00:00", f"2026-08-{10+i:02d}T17:05:00+00:00",
                venue="AWAY", league_id=999, season=2025, stratum="DOMESTIC_CUP", gf=0.0, ga=2.0,
            )
            for i in range(1, 9)
        )
        b0 = VariantSpec("B0", "BASELINE", "ref", shrink_k=8.0, use_kish_neff=False)
        a6a = VariantSpec("A6a", "A6_DECOMP", "kish k=8", shrink_k=8.0, use_kish_neff=True)
        r_b0 = _estimate_side_rate_variant(
            away_comp_other, opp,
            target_league_id=39, target_season=2026, target_stratum="DOMESTIC_LEAGUE",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=1.56, other_venue_prior_mean=1.26, history_limit=20, spec=b0,
        )
        r_a6a = _estimate_side_rate_variant(
            away_comp_other, opp,
            target_league_id=39, target_season=2026, target_stratum="DOMESTIC_LEAGUE",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=1.56, other_venue_prior_mean=1.26, history_limit=20, spec=a6a,
        )
        self.assertAlmostEqual(r_b0["n_eff"], 2.0, places=6)
        self.assertAlmostEqual(r_a6a["n_eff"], 8.0, places=6)
        self.assertGreater(r_a6a["lambda"], r_b0["lambda"])

    def test_candidate_combines_b3_venue_kish_k8_and_c4_identity(self) -> None:
        # UNKNOWN != UNKNOWN en C4 reduce peso de 0.50 a 0.25 respecto a no-C4
        hist_unk = tuple(
            _hm(
                i, 1, 10 + i, f"2026-08-{10+i:02d}T15:00:00+00:00", f"2026-08-{10+i:02d}T17:05:00+00:00",
                venue="HOME" if i % 2 == 0 else "AWAY", league_id=0, season=0, stratum="UNKNOWN", gf=2.0, ga=1.0,
            )
            for i in range(1, 7)
        )
        hist_same = tuple(
            _hm(
                50 + i, 1, 20 + i, f"2026-08-{20+i:02d}T15:00:00+00:00", f"2026-08-{20+i:02d}T17:05:00+00:00",
                venue="HOME", league_id=39, season=2026, stratum="DOMESTIC_LEAGUE", gf=1.0, ga=1.0,
            )
            for i in range(1, 3)
        )
        opp = tuple(
            _hm(
                100 + i, 2, 30 + i, f"2026-08-{10+i:02d}T15:00:00+00:00", f"2026-08-{10+i:02d}T17:05:00+00:00",
                venue="AWAY", league_id=39, season=2026, stratum="DOMESTIC_LEAGUE", gf=1.0, ga=1.5,
            )
            for i in range(1, 7)
        )
        cand_no_c4 = VariantSpec(
            "CAND_NOC4", "INTEGRATION", "no c4",
            shrink_k=8.0, use_kish_neff=True, venue_mode="shrunk_anchor_50", venue_blend=0.50, venue_k=4.0,
            strict_competition_identity=False,
        )
        cand_c4 = VariantSpec(
            "CAND_C4", "INTEGRATION", "cand",
            shrink_k=8.0, use_kish_neff=True, venue_mode="shrunk_anchor_50", venue_blend=0.50, venue_k=4.0,
            strict_competition_identity=True,
        )
        r_noc4 = _estimate_side_rate_variant(
            hist_unk + hist_same, opp,
            target_league_id=0, target_season=0, target_stratum="UNKNOWN",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=1.56, other_venue_prior_mean=1.26, history_limit=20, spec=cand_no_c4,
        )
        r_c4 = _estimate_side_rate_variant(
            hist_unk + hist_same, opp,
            target_league_id=0, target_season=0, target_stratum="UNKNOWN",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=1.56, other_venue_prior_mean=1.26, history_limit=20, spec=cand_c4,
        )
        # En no-C4 los UNKNOWN hacen match entre sí (w=1.0/0.50); en C4 caen a w=0.25 -> distinto lambda
        self.assertNotAlmostEqual(r_noc4["lambda"], r_c4["lambda"], places=4)

        # Verificar además paridad de B0 frente a producción
        b0 = VariantSpec("B0", "BASELINE", "ref")
        r_b0 = _estimate_side_rate_variant(
            hist_unk + hist_same, opp,
            target_league_id=39, target_season=2026, target_stratum="DOMESTIC_LEAGUE",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", "DOMESTIC_LEAGUE", "HOME"),
            other_venue_prior_mean=_resolve_prior("goals", "DOMESTIC_LEAGUE", "AWAY"),
            history_limit=20, spec=b0,
        )
        prod = _estimate_team_rate_detailed(
            hist_unk + hist_same, opp, metric="goals",
            target_league_id=39, target_season=2026, target_stratum="DOMESTIC_LEAGUE",
            own_venue="HOME", opp_venue="AWAY",
            prior_mean=_resolve_prior("goals", "DOMESTIC_LEAGUE", "HOME"),
            history_limit=20,
        )
        self.assertAlmostEqual(float(prod["lambda"]), r_b0["lambda"], places=12)

    def test_murphy_decomposition_fields_and_bounds(self) -> None:
        pairs = [(0.1, 0), (0.15, 0), (0.5, 1), (0.8, 1), (0.85, 0)]
        dec = _murphy_brier_decomp(pairs, n_bins=5)
        self.assertEqual(dec["n"], 5)
        self.assertGreaterEqual(dec["reliability"], 0.0)
        self.assertGreaterEqual(dec["resolution"], 0.0)
        self.assertGreaterEqual(dec["uncertainty"], 0.0)

    def test_audit_confirms_unknown_stratum_maps_to_club_in_asof_elo_tracker(self) -> None:
        # Confirma el defecto de identidad documentado en auditoría de Sección 5 (sin modificar producción)
        fixtures = [
            {
                "fixture_id": 1,
                "home_id": 9,
                "away_id": 10,
                "hg": 3.0,
                "ag": 0.0,
                "stratum": "NATIONAL_TEAM",
                "kickoff_utc": "2026-08-01T18:00:00+00:00",
                "status": "FT",
            },
            {
                "fixture_id": 2,
                "home_id": 9,
                "away_id": 11,
                "hg": 2.0,
                "ag": 0.0,
                "stratum": "UNKNOWN",
                "kickoff_utc": "2026-08-10T18:00:00+00:00",
                "status": "FT",
            },
        ]
        tracker = AsOfEloTracker.build_from_fixtures(fixtures)
        self.assertIn(("NATIONAL_TEAM", 9), tracker.history)
        self.assertIn(("CLUB", 9), tracker.history)


if __name__ == "__main__":
    unittest.main()
