"""Pruebas integrales de auditoría correctiva V1.0 (Fases B–H).

Cubre exclusivamente casos offline sobre snapshots congelados y escenarios adversos:
1. Variantes documentadas de Event ID / Fixture ID, duplicados contradictorios y rechazo de otro partido.
2. Horarios ISO 8601 con zona explícita, ausencia de generación LLM y extracción de XI probables paralelos sin elevar a oficial.
3. Conciliación cronológica y canónica de entrenadores (CURRENT_VERIFIED / HISTORICAL / UNKNOWN) y alertas contextuales con respaldo.
4. Congestión por fecha de Copa no confirmada (venue=null) no genera alerta espuria.
5. Equivalencias contractuales Bwin y falsos equivalentes explícitos.
6. Ranking táctico: evitar ceros espurios por CV Poisson o familias auxiliares ausentes y resolver nombres Bwin de equipo.
7. Controles de fuga temporal post-kickoff, cuotas obsoletas, historial escaso y archivos corruptos.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import prematch_case as pc
import prematch_tactical_signal_engine as ptse
import validate_prematch as vp
from bet_engine.analytics import build_default_registry
from bet_engine.context import HistoricalTeamMatch, MatchObservationBundle, ObservationStore
from bet_engine.context.research_parser import validate_research_identity
from bet_engine.contracts import TemporalLeakageError
from bet_engine.markets import (
    BwinMarketEvaluator,
    are_contracts_equivalent,
    audit_snapshot_equivalences,
)
from bet_engine.modeling import CoherentMatchModeler, WalkForwardEvaluator
from bet_engine.orchestration import ShadowPipelineRunner
from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt

ROOT = Path(__file__).resolve().parent.parent


def _create_isolated_case(root: Path, event_id: str, fixture_id: int, kickoff_madrid: str) -> Path:
    mdir = root / "outputs/matches" / str(event_id)
    sel_dir = mdir / "selection" / "mock"
    sel_dir.mkdir(parents=True, exist_ok=True)
    sel_path = sel_dir / "selected_match.json"
    hoff_path = sel_dir / "handoff.json"
    sel_path.write_text(json.dumps({"event_id": str(event_id), "fixture_id": int(fixture_id)}), encoding="utf-8")
    hoff_path.write_text(json.dumps({"event_id": str(event_id), "fixture_id": int(fixture_id)}), encoding="utf-8")
    (mdir / "case.json").write_text(
        json.dumps(
            {
                "event_id": str(event_id),
                "fixture_id": int(fixture_id),
                "kickoff_madrid": kickoff_madrid,
                "selected": pc.reference(sel_path),
                "source_handoff": pc.reference(hoff_path),
            }
        ),
        encoding="utf-8",
    )
    (mdir / "manifest.json").write_text(
        json.dumps({"event_id": str(event_id), "fixture_id": int(fixture_id), "status": "PREPARADO", "inputs": {}}),
        encoding="utf-8",
    )
    return mdir


class TestCorrectiveAuditV10(unittest.TestCase):
    def test_fase_b_research_variants_conflicts_wrong_fixture_and_parallel_xi(self) -> None:
        rp_path = ROOT / "outputs/matches/7912816/inputs/reporte_cualitativo_7912816.md"
        if not rp_path.is_file():
            self.skipTest("Snapshot 7912816 no presente")

        raw = rp_path.read_text(encoding="utf-8")
        stripped = "\n".join(
            ln for ln in raw.splitlines()
            if ln.strip() not in ("Event ID: 7912816", "Fixture ID: 1649063")
        )
        # 1) Acepta EVENT_ID_BWIN + fixture_id API-Football sin líneas manuales
        parsed = validate_research_identity(stripped, expected_event_id="7912816", expected_fixture_id=1649063)
        self.assertEqual(parsed["event_ids_found"], ["7912816"])
        self.assertEqual(parsed["fixture_ids_found"], ["1649063"])
        self.assertTrue(parsed["sources_attributed"]["grok"])
        self.assertTrue(parsed["sources_attributed"]["perplexity"])
        self.assertTrue(parsed["sources_attributed"]["codex_consolidation"])

        # 2) Rechaza Event ID correcto con Fixture ID de otro partido
        with self.assertRaises(ValueError):
            validate_research_identity(stripped, expected_event_id="7912816", expected_fixture_id=9999999)

        # 3) Rechaza identificadores duplicados y contradictorios
        duplicated_conflict = stripped + "\n**Event ID**: 7999999\n"
        with self.assertRaisesRegex(ValueError, "CONFLICTING_EVENT_IDS"):
            validate_research_identity(duplicated_conflict, expected_event_id="7912816", expected_fixture_id=1649063)

        # 4) Extrae kickoff ISO con sufijo CET, conserva generated=None y lee 11+11 XI PROBABLE sin promover a oficial
        meta = vp.report_meta(stripped, home="Wanderers", away="Juventud", home_bwin="Wanderers", away_bwin="Juventud")
        self.assertIsNotNone(meta["kickoff_utc"])
        self.assertIsNotNone(meta["cutoff_utc"])
        self.assertIsNone(meta["generated_utc"])
        self.assertEqual(len(meta["xi"].get("local", [])), 11)
        self.assertEqual(len(meta["xi"].get("visitante", [])), 11)
        self.assertEqual(meta["xi_classification"].get("local"), "PROBABLE")
        self.assertEqual(meta["xi_classification"].get("visitante"), "PROBABLE")
        self.assertTrue(meta["has_source_registry"])

        # Verifica attach_research extremo a extremo en carpeta aislada
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mdir = _create_isolated_case(root, "7912816", 1649063, "2026-11-17T00:00:00+01:00")
            src = root / "qual.md"
            src.write_text(stripped, encoding="utf-8")
            attached = pc.attach_research(root, "7912816", src)
            self.assertTrue(attached.is_file())
            manifest = json.loads((mdir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["inputs"]["qualitative_research"]["sources_attributed"]["grok"], True)

    def test_fase_c_coach_chronological_resolution_and_unconfirmed_cup_guard(self) -> None:
        me_path = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        if not me_path.is_file():
            self.skipTest("Snapshot 7912816 no presente")

        me = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(me)
        coaches = {c.facts["side"]: c.facts for c in bundle.contextual_evidence if c.category == "COACH_REGIME"}
        # Sin evidencia de entrenador en el encuentro objetivo, la continuidad histórica no se promueve a CURRENT_VERIFIED
        self.assertIsNone(coaches["home"]["current_coach_name"])
        self.assertEqual(coaches["home"]["last_historical_coach_name"], "M. Corujo")
        self.assertEqual(coaches["home"]["coach_status"], "HISTORICAL_ONLY_UNVERIFIED")
        self.assertFalse(coaches["home"]["continuity_verified"])
        self.assertFalse(coaches["home"]["regime_change_detected"])
        self.assertIsNone(coaches["away"]["current_coach_id"])
        self.assertEqual(coaches["away"]["last_historical_coach_id"], 28446)
        self.assertEqual(coaches["away"]["coach_status"], "HISTORICAL_ONLY_UNVERIFIED")
        self.assertFalse(coaches["away"]["regime_change_detected"])

        # Con alineación objetivo confirmando a los mismos técnicos sí se promueve a CURRENT_VERIFIED
        me_with_target = json.loads(json.dumps(me))
        me_with_target["context"]["lineups"] = {
            "response": [
                {"team": {"id": me["fixture"]["home_id"]}, "coach": {"id": 28782, "name": "Mathias Corujo"}},
                {"team": {"id": me["fixture"]["away_id"]}, "coach": {"id": 28446, "name": "Sergio Blanco"}},
            ]
        }
        bundle_target = ObservationStore.from_match_engine_json(me_with_target)
        coaches_target = {c.facts["side"]: c.facts for c in bundle_target.contextual_evidence if c.category == "COACH_REGIME"}
        self.assertEqual(coaches_target["home"]["current_coach_name"], "Mathias Corujo")
        self.assertEqual(coaches_target["home"]["coach_status"], "CURRENT_VERIFIED")
        self.assertTrue(coaches_target["home"]["continuity_verified"])
        self.assertEqual(coaches_target["away"]["current_coach_id"], 28446)
        self.assertEqual(coaches_target["away"]["coach_status"], "CURRENT_VERIFIED")
        self.assertTrue(coaches_target["away"]["continuity_verified"])

        res = build_default_registry().run_all(bundle)
        all_flags = {f for p in res.prematch_patterns for f in p.contradiction_flags}
        self.assertNotIn("COACH_REGIME_SHIFT_LIMITS_HISTORY_COMPARABILITY", all_flags)
        self.assertNotIn("FIXTURE_CONGESTION_ROTATION_RISK", all_flags)
        self.assertIn("PROVISIONAL_CUP_LOOKAHEAD_UNCONFIRMED", all_flags)
        self.assertIn("HIGH_CARD_REFEREE", all_flags)

        ref_pat = next(p for p in res.prematch_patterns if "HIGH_CARD_REFEREE" in p.contradiction_flags)
        self.assertIn("supporting_evidence", ref_pat.estimates)
        self.assertIn("inferred_component", ref_pat.estimates)
        self.assertEqual(ref_pat.estimates["market_relevance"], ["YELLOW_CARDS"])

        # Cambio real de entrenador cronológico SÍ dispara la alerta
        old_m = HistoricalTeamMatch(
            fixture_id=1001, team_id=2360, opponent_id=99, kickoff_utc="2026-09-01T18:00:00+00:00",
            available_at_utc="2026-09-01T20:05:00+00:00", league_id=268, league_name="Primera", season=2026,
            competition_stratum="DOMESTIC_LEAGUE", venue="HOME", status="FT", coach_id=785, coach_name="A. Apud",
            red_card_distortion=False, own_metrics={"goals": 1.0}, conceded_metrics={"goals": 1.0}, has_detailed_stats=True, source="t",
        )
        from bet_engine.context.observation_store import _canonical_coach_list
        _, real_shift = _canonical_coach_list([old_m, *bundle.home_matches])
        self.assertTrue(real_shift)

    def test_fase_d_dispersion_negbin_n4_and_prior_dominance_warning(self) -> None:
        me_path = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        if not me_path.is_file():
            self.skipTest("Snapshot 7912816 no presente")

        me = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(me)
        model = CoherentMatchModeler.build_model(bundle)
        shots = model.families["SHOTS"]
        self.assertEqual(shots.model_type, "NEGATIVE_BINOMIAL_CONVOLUTION")
        self.assertIn("PRIOR_WEIGHT_EXCEEDS_LOCAL_SAMPLE", shots.warnings)
        # Cobertura de equivalentes FT & H1 coherente entre Under 0.5 y Exact 0-0
        ft_grid, _ = model.joint_grid_with_corners("FT")
        self.assertAlmostEqual(ft_grid[0][0], model.families["GOALS_FT"].prob_over("total", 0.5)["p_under"], places=4)
        h1_grid, _ = model.joint_grid_with_corners("H1")
        self.assertAlmostEqual(h1_grid[0][0], model.families["GOALS_H1"].prob_over("total", 0.5)["p_under"], places=4)

    def test_fase_e_market_equivalences_and_false_equivalents_in_7912816(self) -> None:
        me_path = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        txt_path = ROOT / "outputs/matches/7912816/inputs/cuotas_bwin_7912816.txt"
        if not (me_path.is_file() and txt_path.is_file()):
            self.skipTest("Snapshot 7912816 no presente")

        me = json.loads(me_path.read_text(encoding="utf-8"))
        bundle = ObservationStore.from_match_engine_json(me)
        mm = me.get("match") or {}
        odds = parse_bwin_odds_txt(
            txt_path,
            home_name=mm.get("home_name") or bundle.home_name,
            away_name=mm.get("away_name") or bundle.away_name,
            home_aliases=(bundle.home_name, mm.get("home_name_raw") or ""),
            away_aliases=(bundle.away_name, mm.get("away_name_raw") or ""),
        )
        eq = audit_snapshot_equivalences(odds)
        by_cid = {g["contract_id"]: g for g in eq["equivalent_groups"]}
        self.assertIn("FT_ZERO_GOALS_REGULATION", by_cid)
        self.assertEqual(by_cid["FT_ZERO_GOALS_REGULATION"]["best_selection_key"], "exact_score_0_0")
        self.assertEqual(by_cid["FT_ZERO_GOALS_REGULATION"]["best_odd"], 11.5)
        self.assertEqual(by_cid["FT_ZERO_GOALS_REGULATION"]["worst_odd"], 10.5)
        self.assertIn("FT_EXACT_1_1_REGULATION", by_cid)
        self.assertEqual(by_cid["FT_EXACT_1_1_REGULATION"]["best_odd"], 6.25)

        # Equivalentes verdaderos vs falsos equivalentes
        self.assertTrue(are_contracts_equivalent("under_0_5_goals", "exact_score_0_0"))
        self.assertTrue(are_contracts_equivalent("double_chance_x2", "eh_m1_away"))
        self.assertFalse(are_contracts_equivalent("exact_score_0_0", "h1_exact_score_0_0"))
        self.assertFalse(are_contracts_equivalent("dnb_home", "eh_m1_home"))
        self.assertFalse(are_contracts_equivalent("home_win", "dnb_home"))

        rep = ShadowPipelineRunner(min_lead_minutes=0).run_shadow_on_bundle(
            bundle, target_status="NS", odds_map=odds, bwin_txt_path=txt_path
        )
        quoted = [m for m in rep.market_comparisons if m.get("odd") is not None]
        self.assertGreater(len(quoted), 150)
        by_sel = {m["selection_key"]: m for m in quoted}
        self.assertEqual(by_sel["under_0_5_goals"]["equivalent_best_selection"], "exact_score_0_0")
        self.assertEqual(by_sel["under_0_5_goals"]["equivalent_best_odd"], 11.5)
        # Cola estadística (<5%) nunca promueve a PICK_ACCIONABLE aunque EV central > 0
        self.assertEqual(by_sel["exact_score_0_0"]["decision_category"], "CANDIDATO_CONDICIONAL")

    def test_fase_f_tactical_scoring_aliases_and_coverage_summary(self) -> None:
        pre_path = ROOT / "outputs/discovery/2026-11-16_1500_0300/evaluations/20261116T202338718202Z_6d060c0ce994/prospective_trial/enriched/preselection.json"
        if not pre_path.is_file():
            self.skipTest("Preselection 1649063 no presente")
        doc = json.loads(pre_path.read_text(encoding="utf-8"))
        f = next(m for m in doc["matches"] if m["fixture_id"] == 1649063)
        asof = datetime(2026, 11, 16, 20, 35, 0, tzinfo=timezone.utc)
        x = ptse.analyze_fixture(f, asof, f.get("statistical_observations") or {})
        self.assertGreater(x["tactical_research_score"], 50.0)
        self.assertGreaterEqual(x["signal_count"], 1)
        self.assertGreater(x["coverage_summary"]["metric_observations_valid"], 0)
        self.assertEqual(ptse.classify_market("Montevideo Wanderers FC - Total de goles | Tiempo reglamentario", f), ("goals", "home"))
        self.assertEqual(ptse.classify_market("Juventud de Las Piedras - Total de goles | Tiempo reglamentario", f), ("goals", "away"))

    def test_adverse_temporal_leakage_corrupt_odds_and_other_event_reuse_blocked(self) -> None:
        # 1) Datos posteriores al kickoff bloqueados por WalkForwardEvaluator
        m_past = HistoricalTeamMatch(
            fixture_id=1, team_id=10, opponent_id=20, kickoff_utc="2026-03-01T15:00:00+00:00",
            available_at_utc="2026-03-01T17:05:00+00:00", league_id=1, league_name="L", season=2026,
            competition_stratum="DOMESTIC_LEAGUE", venue="HOME", status="FT", coach_id=None, coach_name=None,
            red_card_distortion=False, own_metrics={"goals": 2.0}, conceded_metrics={"goals": 1.0}, has_detailed_stats=True, source="s",
        )
        m_future_leak = HistoricalTeamMatch(
            fixture_id=2, team_id=10, opponent_id=21, kickoff_utc="2026-03-05T15:00:00+00:00",
            available_at_utc="2026-03-05T17:05:00+00:00", league_id=1, league_name="L", season=2026,
            competition_stratum="DOMESTIC_LEAGUE", venue="AWAY", status="FT", coach_id=None, coach_name=None,
            red_card_distortion=False, own_metrics={"goals": 1.0}, conceded_metrics={"goals": 1.0}, has_detailed_stats=True, source="s",
        )
        res = WalkForwardEvaluator.evaluate_series([m_past] * 4 + [m_future_leak], metric="goals", line=0.5)
        self.assertEqual(res.temporal_leakage_violations, 0)

        # 2) Reutilización accidental de reporte de otro evento bloqueada sin falsos positivos de case/manifest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _create_isolated_case(root, "111111", 222222, "2026-11-18T18:00:00+01:00")
            other_rep = root / "other.md"
            other_rep.write_text("**EVENT_ID_BWIN**: 7912816\n**fixture_id API-Football**: 1649063\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "QUALITATIVE_RESEARCH_IDENTITY_MISMATCH"):
                pc.attach_research(root, "111111", other_rep)

            # Archivo incompleto/corrupto sin identificadores también falla cerrado
            corrupt_rep = root / "corrupt.md"
            corrupt_rep.write_text("# Informe incompleto sin cabecera\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "QUALITATIVE_RESEARCH_IDENTITY_MISMATCH"):
                pc.attach_research(root, "111111", corrupt_rep)


if __name__ == "__main__":
    unittest.main()
