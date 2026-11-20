"""Pruebas unitarias y Walk-Forward de la Segunda Etapa:
1. Deduplicación de fixtures H2H compartidos (sin doble conteo en varianza ni total P-6).
2. Incertidumbre estadística por familia (Gamma-Quasi-Poisson/NegBin/Poisson/Dixon-Coles), sin bandas planas ±17 pp.
3. Conservación de 0/N histórico como evidencia de riesgo explícita (no veto absoluto).
4. Validación Walk-Forward sin fuga temporal, calibración de intervalos por cuantiles y comparación Antes/Después.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bet_engine.context import HistoricalTeamMatch, MatchObservationBundle, ObservationStore
from bet_engine.markets import BwinMarketEvaluator
from bet_engine.modeling import CoherentMatchModeler, WalkForwardEvaluator
from bet_engine.modeling.calibration import brier_decomposition, evaluate_interval_calibration
from bet_engine.orchestration import ShadowPipelineRunner
from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt

ROOT = Path(__file__).resolve().parent.parent


def _mk_hist_match(
    fixture_id: int,
    team_id: int,
    opp_id: int,
    day: int,
    venue: str,
    own_goals: float,
    conc_goals: float,
    *,
    league_id: int = 100,
    season: int = 2026,
    stratum: str = "DOMESTIC_LEAGUE",
) -> HistoricalTeamMatch:
    ko = f"2026-03-{day:02d}T15:00:00+00:00"
    av = f"2026-03-{day:02d}T17:05:00+00:00"
    return HistoricalTeamMatch(
        fixture_id=fixture_id,
        team_id=team_id,
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
        own_metrics={"goals": own_goals, "corners": 5.0, "yellow": 2.0},
        conceded_metrics={"goals": conc_goals, "corners": 4.0, "yellow": 2.0},
        has_detailed_stats=True,
        source="synthetic_test",
    )


class TestStage2ProbabilisticAudit(unittest.TestCase):
    def test_h2h_deduplication_in_model_and_legacy_p6(self) -> None:
        me_path = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.json"
        txt_path = ROOT / "outputs/matches/7909704/inputs/cuotas_bwin_7909704.txt"
        if not (me_path.is_file() and txt_path.is_file()):
            self.skipTest("Snapshot 7909704 no presente")

        payload = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload)
        match_meta = payload.get("match") or {}
        odds_map = parse_bwin_odds_txt(
            txt_path,
            home_name=match_meta.get("home_name") or bundle.home_name,
            away_name=match_meta.get("away_name") or bundle.away_name,
            home_aliases=(bundle.home_name, match_meta.get("home_name_raw") or ""),
            away_aliases=(bundle.away_name, match_meta.get("away_name_raw") or ""),
        )
        model = CoherentMatchModeler.build_model(bundle)
        goals_ft = model.families["GOALS_FT"]
        self.assertEqual(goals_ft.shared_h2h_n, 1)
        self.assertIn("SHARED_H2H_DEDUPLICATED_1", goals_ft.warnings)

        opts = BwinMarketEvaluator.evaluate_standard_markets(bundle, model, odds_map=odds_map)
        by_key = {o.selection_key: o for o in opts}
        # Antes duplicaba el H2H 1528915 dando 12 en goles y 10 en córners; ahora son 11 y 9 únicos.
        self.assertEqual(by_key["over_2_5_goals"].legacy_p6_n, 11)
        self.assertEqual(by_key["over_7_5_corners"].legacy_p6_n, 9)

        # Verificación sintética directa con 6 H2H compartidos
        shared_home = tuple(
            _mk_hist_match(900 + i, 1, 2, i + 1, "HOME" if i % 2 == 0 else "AWAY", 2.0, 1.0)
            for i in range(6)
        )
        shared_away = tuple(
            _mk_hist_match(900 + i, 2, 1, i + 1, "AWAY" if i % 2 == 0 else "HOME", 1.0, 2.0)
            for i in range(6)
        )
        syn_bundle = MatchObservationBundle(
            event_id="syn_h2h",
            fixture_id=99999,
            league_id=100,
            season=2026,
            kickoff_utc="2026-04-01T15:00:00+00:00",
            cutoff_utc="2026-04-01T14:00:00+00:00",
            home_id=1,
            away_id=2,
            home_name="Home",
            away_name="Away",
            home_matches=shared_home,
            away_matches=shared_away,
            facts=(),
            contextual_evidence=(),
            rejected_records=(),
            windows={},
            source_sha256="0" * 64,
        )
        syn_model = CoherentMatchModeler.build_model(syn_bundle)
        self.assertEqual(syn_model.families["GOALS_FT"].shared_h2h_n, 6)
        self.assertEqual(syn_model.families["GOALS_FT"].unique_fixture_n, 6)

    def test_family_appropriate_asymmetric_uncertainty_no_flat_bands(self) -> None:
        me_path = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.json"
        if not me_path.is_file():
            self.skipTest("Snapshot 7909704 no presente")
        payload = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload)
        model = CoherentMatchModeler.build_model(bundle)

        # Bandas en colas vs centro son distintas y asimétricas (sin ±0.1701 constante)
        gdist = model.families["GOALS_FT"]
        p_tail = gdist.prob_over("total", 0.5)
        p_mid = gdist.prob_over("total", 2.5)
        w_tail = round(p_tail["p_over_high"] - p_tail["p_over_low"], 4)
        w_mid = round(p_mid["p_over_high"] - p_mid["p_over_low"], 4)
        self.assertNotAlmostEqual(w_tail, 0.3402, places=3)
        self.assertLess(w_tail, w_mid)
        self.assertLessEqual(p_mid["p_over_low"], p_mid["p_over"])
        self.assertLessEqual(p_mid["p_over"], p_mid["p_over_high"])

        # Tarjetas amarillas no fuerzan Binomial Negativa si phi <= 1.20
        ydist = model.families["YELLOW_CARDS"]
        self.assertEqual(ydist.model_type, "POISSON_CONVOLUTION")

        # 1X2 / Draw No Bet tienen intervalos propagados vía Dixon-Coles y coherencia en [0, 1]
        draw_lo, draw_hi = model.outcomes_1x2_bounds["draw"]
        hw_lo, hw_hi = model.outcomes_1x2_bounds["home_win"]
        self.assertLess(draw_hi - draw_lo, hw_hi - hw_lo)

    def test_zero_of_n_is_risk_evidence_not_absolute_veto_and_short_sample_stays_no_bet(self) -> None:
        me_path = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.json"
        txt_path = ROOT / "outputs/matches/7909704/inputs/cuotas_bwin_7909704.txt"
        if not (me_path.is_file() and txt_path.is_file()):
            self.skipTest("Snapshot 7909704 no presente")
        payload = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(payload)
        match_meta = payload.get("match") or {}
        odds_map = parse_bwin_odds_txt(
            txt_path,
            home_name=match_meta.get("home_name") or bundle.home_name,
            away_name=match_meta.get("away_name") or bundle.away_name,
        )
        report = ShadowPipelineRunner(min_lead_minutes=0).run_shadow_on_bundle(
            bundle, target_status="NS", odds_map=odds_map, bwin_txt_path=txt_path
        )
        by_key = {m["selection_key"]: m for m in report.market_comparisons}
        h_o15 = by_key["home_over_1_5_goals"]
        self.assertEqual(h_o15["legacy_p6_hit_rate"], 0.0)
        self.assertNotEqual(h_o15["decision_category"], "NO_BET_POR_EVIDENCIA")
        self.assertNotEqual(h_o15["decision_category"], "PICK_ACCIONABLE")
        self.assertTrue(any("RIESGO_HISTORICO_0_DE_6" in r for r in h_o15["risk_flags"]))

        # Muestra realmente insuficiente (<4 partidos) SÍ permanece en NO_BET_POR_EVIDENCIA
        short_bundle = MatchObservationBundle(
            event_id="short_n2",
            fixture_id=88888,
            league_id=100,
            season=2026,
            kickoff_utc="2026-04-01T15:00:00+00:00",
            cutoff_utc="2026-04-01T14:00:00+00:00",
            home_id=1,
            away_id=2,
            home_name="H",
            away_name="A",
            home_matches=tuple(_mk_hist_match(10 + i, 1, 3 + i, i + 1, "HOME", 1.0, 1.0) for i in range(2)),
            away_matches=tuple(_mk_hist_match(20 + i, 2, 5 + i, i + 1, "AWAY", 1.0, 1.0) for i in range(2)),
            facts=(),
            contextual_evidence=(),
            rejected_records=(),
            windows={},
            source_sha256="0" * 64,
        )
        short_model = CoherentMatchModeler.build_model(short_bundle)
        short_opts = BwinMarketEvaluator.evaluate_standard_markets(
            short_bundle, short_model, odds_map={"over_2_5_goals": 3.50}
        )
        short_o25 = {o.selection_key: o for o in short_opts}["over_2_5_goals"]
        self.assertEqual(short_o25.decision_category, "NO_BET_POR_EVIDENCIA")

    def test_walk_forward_stage1_vs_stage2_and_interval_calibration_stratified(self) -> None:
        src = ROOT / "outputs/reconciliation_real_20261111/coverage_models/preselection_cache_recovered.json"
        if not src.is_file():
            self.skipTest("Corpus preselection_cache_recovered.json no presente")
        data = json.loads(src.read_text(encoding="utf-8"))
        items = data.get("matches") or data.get("candidates") if isinstance(data, dict) else data
        bundles = []
        seen = set()
        for item in (items or [])[:120]:
            if not isinstance(item, dict):
                continue
            fid = item.get("fixture_id")
            cut = item.get("analysis_cutoff_utc") or item.get("kickoff_utc") or item.get("kickoff_madrid")
            if not fid or not cut or fid in seen:
                continue
            b = ObservationStore.from_preselection_match(item, cutoff_utc=cut)
            if b.home_matches or b.away_matches:
                seen.add(fid)
                bundles.append(b)

        comp = WalkForwardEvaluator.evaluate_stage1_vs_stage2(bundles, metric="goals", line=1.5)
        self.assertGreater(comp["out_of_sample_n"], 200)
        self.assertEqual(comp["temporal_leakage_violations"], 0)
        self.assertLessEqual(comp["stage2"]["brier_decomp"]["brier"], comp["stage1"]["brier_decomp"]["brier"])
        self.assertLessEqual(comp["stage2"]["log_loss"], comp["stage1"]["log_loss"])
        self.assertLess(comp["stage2"]["ece"], comp["stage1"]["ece"])
        self.assertGreaterEqual(comp["stage2"]["interval_calibration"]["se_adjusted_bin_coverage"], 0.8)

        # Verificar que evaluate_interval_calibration evalúa cobertura de tasa estratificada, no {0,1} individual
        iv_check = evaluate_interval_calibration(
            [(0.5, 0.35, 0.65, i % 2) for i in range(100)],
            n_bins=5,
        )
        self.assertEqual(iv_check["raw_bin_coverage"], 1.0)
        decomp = brier_decomposition([(0.5, i % 2) for i in range(100)])
        self.assertAlmostEqual(decomp["brier"], 0.25, places=4)


if __name__ == "__main__":
    unittest.main()
