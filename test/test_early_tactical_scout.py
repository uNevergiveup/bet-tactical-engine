import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bet_engine.analytics.early_tactical_scout import (
    EarlyScoutError,
    build_research_handoff,
    finalize_early_scout,
    prepare_early_scout_package,
    run_early_scout_with_analyst,
)


class ResearchHandoffTests(unittest.TestCase):
    def test_cli_routes_bounded_research_and_reuses_sources(self):
        import prematch_steps as steps

        fixture = {**_base_identity(), "bwin_event_id": "7901446"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sources.json"
            entry = {"url": "https://club.example/previa", "claim": "Nota anterior",
                     "published_at_utc": "2026-11-19T17:00:00Z", "available_at_utc": "2026-11-19T18:00:00Z"}
            registry.write_text(json.dumps({
                "fixture_id": 1492457, "event_id": "7901446",
                "sources": [{**entry, "provider": "grok"}, {**entry, "provider": "perplexity",
                             "url": entry["url"] + "?utm_source=news#intro"},
                            {**entry, "url": "https://club.example/future", "claim": "EVIDENCIA_FUTURA",
                             "available_at_utc": "2026-11-20T00:25:00Z"}],
            }))
            with patch.object(steps, "_resolve_eval_dir", return_value=(root, root, None, None)), patch.object(
                steps, "_resolve_target_rec_from_eval", return_value=fixture
            ) as resolver:
                common = ["early-scout", "--fixture-id", "1492457", "--output-dir", tmp,
                          "--research-question", "¿Cómo protege el carril central?"]
                self.assertEqual(steps.main(common + ["--asof", "2026-11-19T19:30:00Z",
                                                      "--research-phase", "deep", "--source-registry", str(registry)]), 0)
                deep = next(root.glob("research_deep_*/research_handoff.json"))
                handoff = json.loads(deep.read_bytes())
                self.assertEqual((handoff["provider"], handoff["execution_mode"]), ("gemini", "REPLAY"))
                self.assertEqual(len(handoff["source_registry"]), 2)
                self.assertNotIn("EVIDENCIA_FUTURA", deep.with_name("research_gemini.md").read_text())
                self.assertEqual(steps.main(common + ["--asof", "2026-11-20T00:10:00Z", "--research-phase",
                                                      "update", "--research-provider", "grok", "--source-registry", str(deep)]), 0)
                self.assertTrue(list(root.glob("research_update_*/research_grok.md")))
                self.assertEqual(resolver.call_args.kwargs["min_lead_minutes"], 15)

    def test_deep_window_and_registry_identity(self):
        identity = _base_identity()
        kickoff = datetime.fromisoformat(identity["kickoff_utc"])
        for minutes in (240, 360):
            handoff = build_research_handoff(identity=identity, asof_utc=kickoff-timedelta(minutes=minutes),
                                             phase="deep", questions=["Presión"])
            self.assertEqual(handoff["provider"], "gemini")
        with self.assertRaisesRegex(EarlyScoutError, "OUTSIDE_WINDOW"):
            build_research_handoff(identity=identity, asof_utc=kickoff-timedelta(minutes=239),
                                   phase="deep", questions=["Presión"])
        with self.assertRaisesRegex(EarlyScoutError, "IDENTITY_MISMATCH"):
            build_research_handoff(identity=identity, asof_utc=kickoff-timedelta(hours=5),
                                   phase="deep", questions=["Presión"], sources={"fixture_id": 999})


def _base_identity(fid: int = 1492457, eid: str = "7901446") -> dict:
    return {
        "fixture_id": fid,
        "event_id": eid,
        "home": "Sao Paulo",
        "away": "Fluminense",
        "country": "Brazil",
        "league": "Serie A",
        "kickoff_utc": "2026-11-20T00:30:00Z",
        "kickoff_madrid": "2026-11-20T01:30:00+01:00",
    }


def _valid_ai_analysis() -> dict:
    return {
        "coaches_and_systems": {"home": "3-5-2 probable", "away": "4-2-3-1 probable"},
        "players": {"key_absences": ["Newton (suspendido)", "Canobbio (CONTRADICTORIO)"]},
        "tactical_matchup": {
            "primary_mechanism": "Banda derecha de Sao Paulo sin Newton expuesta a extremos de Fluminense.",
            "counter_scenario": "Fluminense prioriza posesion conservadora sin atacar la espalda.",
        },
        "competitive_context": {"note": "Jornada 35 Brasileirao"},
        "hypotheses": [
            {
                "tactical_mechanism": "Ataques exteriores sobre el carril derecho de Sao Paulo tras la baja de Newton.",
                "team_and_players": "Fluminense (Serna/Soteldo) vs costado derecho de Sao Paulo",
                "necessary_conditions": "Fluminense logra progresar por banda y sostener posesion en campo rival.",
                "supporting_stats": "Sao Paulo con rotacion forzada en linea de tres centrales.",
                "contradictory_evidence": "Contradiccion sobre disponibilidad de Canobbio entre ge y Itatiaia.",
                "opponent_coach_response": "Cerrar con Pablo Maia ayudando al carril derecho.",
                "invalidation_scenarios": "Bloque ultra bajo de Sao Paulo sin ceder banda o gol temprano local.",
                "related_market_family": "corners_visitante_o_remates",
                "evidence_quality": "PROBABLE",
                "pending_info": "Confirmacion oficial de XI a T-60.",
            }
        ],
    }


class TestEarlyTacticalScout(unittest.TestCase):
    def test_1_identity_mismatch_external_report_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            bad_report = "# Previa\n- fixture_id: 9999999\n- event_id: 8888888\n- Partido: Arsenal vs Chelsea\n- analysis_cutoff_utc: 2026-11-19T20:00:00Z\n"
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                external_reports=[{"source": "perplexity", "raw_text": bad_report}],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            si = res["scout_input"]
            self.assertEqual(len(si["external_reports"]["accepted"]), 0)
            self.assertEqual(si["external_reports"]["rejected"][0]["reason"], "IDENTITY_MISMATCH")
            self.assertIn("IDENTITY_MISMATCH:perplexity", si["coverage"]["warnings"])

    def test_2_missing_verifiable_timestamp_downgrades_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            no_ts = "# Previa\n- fixture_id: 1492457\n- event_id: 7901446\n- Partido: Sao Paulo vs Fluminense\n"
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                external_reports=[{"source": "gemini", "raw_text": no_ts}],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            si = res["scout_input"]
            self.assertEqual(len(si["external_reports"]["accepted"]), 1)
            self.assertEqual(si["external_reports"]["accepted"][0]["timestamp_status"], "NO_VERIFICADO")
            self.assertIn("MISSING_VERIFIABLE_TIMESTAMP:gemini", si["coverage"]["warnings"])

    def test_3_probable_xi_never_promoted_to_confirmed(self):
        with tempfile.TemporaryDirectory() as td:
            xi_md = (
                "# Previa\n"
                "- fixture_id: 1492457\n- event_id: 7901446\n"
                "- analysis_cutoff_utc: 2026-11-19T23:00:00Z\n\n"
                "## Alineaciones probables\n"
                "| N. | Sao Paulo — PROBABLE | Fluminense — PROBABLE |\n"
                "|---|---|---|\n"
                "| 1 | Rafael | Fabio |\n"
                "| 9 | Calleri | Cano |\n"
            )
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                external_reports=[{"source": "perplexity", "raw_text": xi_md}],
                asof_utc=datetime(2026, 11, 19, 23, 30, tzinfo=timezone.utc),
            )
            lineups = res["scout_input"]["external_reports"]["accepted"][0]["lineups"]
            self.assertEqual(lineups["local"]["epistemic_status"], "PROBABLE")
            self.assertEqual(lineups["visitante"]["epistemic_status"], "PROBABLE")

    def test_4_historical_only_player_not_accredited_in_current_squad(self):
        with tempfile.TemporaryDirectory() as td:
            pre_match = {
                "home_evidence": {"historical_players": ["Jugador Historico Antiguo"]},
                "away_evidence": {"historical_players": []},
            }
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                pre_match=pre_match,
                asof_utc=datetime(2026, 11, 19, 23, 30, tzinfo=timezone.utc),
            )
            guard = res["scout_input"]["historical_player_guard"]
            self.assertIn("jugador historico antiguo", guard["historical_unaccredited_players"])
            self.assertEqual(guard["status"], "NO_VERIFICADO_PLANTILLA_ACTUAL")

    def test_5_contradictory_injuries_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            rep1 = (
                "- fixture_id: 1492457\n- event_id: 7901446\n- analysis_cutoff_utc: 2026-11-19T22:00:00Z\n"
                "| Jugador | Estado |\n|---|---|\n| Canobbio | Ausencia por control de carga |\n"
            )
            rep2 = (
                "- fixture_id: 1492457\n- event_id: 7901446\n- analysis_cutoff_utc: 2026-11-19T22:30:00Z\n"
                "| Jugador | Estado |\n|---|---|\n| Canobbio | Disponible y probable titular |\n"
            )
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                external_reports=[
                    {"source": "ge", "raw_text": rep1},
                    {"source": "itatiaia", "raw_text": rep2},
                ],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            contras = res["scout_input"]["external_reports"]["contradictions"]
            self.assertTrue(any(c["player"] == "canobbio" and c["status"] == "CONTRADICTORIO" for c in contras))

    def test_6_missing_data_or_incomplete_caches_degrade_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                pre_match=None,
                tactical_fx=None,
                external_reports=[],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            cov = res["scout_input"]["coverage"]
            self.assertEqual(cov["coverage_level"], "MINIMAL_INSUFFICIENT_DATA")
            self.assertIn("api_football_preselection", cov["missing_sources"])
            self.assertIn("external_qualitative_report", cov["missing_sources"])

    def test_7_post_kickoff_contamination_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            post_ko = (
                "- fixture_id: 1492457\n- event_id: 7901446\n"
                "- report_generated_utc: 2026-11-20T02:45:00Z\n"
                "Resultado final 2-1.\n"
            )
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                external_reports=[{"source": "postmatch", "raw_text": post_ko}],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(len(res["scout_input"]["external_reports"]["accepted"]), 0)
            self.assertEqual(res["scout_input"]["external_reports"]["rejected"][0]["reason"], "POST_KICKOFF_CONTAMINATION")

    def test_8_execution_without_external_report_and_no_odds(self):
        class StubAnalyst:
            def analyze(self, scout_input, prompt_markdown):
                assert scout_input["odds_dependency"] is False
                return _valid_ai_analysis()

        with tempfile.TemporaryDirectory() as td:
            out = run_early_scout_with_analyst(
                identity=_base_identity(),
                output_dir=Path(td),
                analyst=StubAnalyst(),
                pre_match={"home_evidence": {"n": 10}, "away_evidence": {"n": 10}},
                external_reports=[],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            self.assertFalse(out["report"]["odds_dependency"])
            self.assertEqual(out["report"]["coverage"]["coverage_level"], "PARTIAL_STATS_ONLY_NO_EXTERNAL_REPORT")
            self.assertTrue(Path(out["report_json_path"]).is_file())
            self.assertTrue(Path(out["report_md_path"]).is_file())

    def test_9_isolation_between_matches(self):
        with tempfile.TemporaryDirectory() as td:
            d1 = Path(td) / "7901446"
            d2 = Path(td) / "7913441"
            r1 = prepare_early_scout_package(identity=_base_identity(1492457, "7901446"), output_dir=d1, asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc))
            r2 = prepare_early_scout_package(identity=_base_identity(1638286, "7913441"), output_dir=d2, asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc))
            self.assertNotEqual(r1["input_json_path"], r2["input_json_path"])
            self.assertEqual(json.loads(Path(r1["input_json_path"]).read_text())["identity"]["fixture_id"], 1492457)
            self.assertEqual(json.loads(Path(r2["input_json_path"]).read_text())["identity"]["fixture_id"], 1638286)

    def test_10_phase1_forbids_probabilities_ev_or_bet_authorization(self):
        with tempfile.TemporaryDirectory() as td:
            prep = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            bad_ai = _valid_ai_analysis()
            bad_ai["actionable_bet"] = True
            bad_ai["p_model"] = 0.62
            with self.assertRaises(EarlyScoutError):
                finalize_early_scout(scout_input=prep["scout_input"], analysis=bad_ai, output_dir=Path(td))

    def test_11_large_context_over_8000_chars_preserves_tactical_cache_stats(self):
        with tempfile.TemporaryDirectory() as td:
            big_filler = "Analisis de contexto previo. " * 400  # > 11,000 chars
            rep_md = (
                "- fixture_id: 1492457\n- event_id: 7901446\n- analysis_cutoff_utc: 2026-11-19T22:00:00Z\n"
                + big_filler
            )
            pre_match = {
                "home_evidence": {
                    "history": {"completed_total": 20, "goals_for": {"n": 20, "mean": 1.05}},
                    "details": {"fixture_details_available": 0, "own": {"corners": {"n": 0, "mean": None}}},
                },
                "away_evidence": {
                    "history": {"completed_total": 20, "goals_for": {"n": 20, "mean": 1.60}},
                    "details": {"fixture_details_available": 0, "own": {"corners": {"n": 0, "mean": None}}},
                },
            }
            tactical_fx = {
                "signals": [
                    {
                        "team_side": "home",
                        "metric": "corners",
                        "priority_score": 99.9,
                        "own": {"n": 9, "mean": 5.88, "stdev": 3.1, "cv": 0.52, "venue": {"n": 5, "mean": 6.4, "venue_filter": "HOME"}},
                        "opponent_conceded": {"n": 8, "mean": 5.5, "stdev": 2.4, "cv": 0.43},
                    },
                    {
                        "team_side": "away",
                        "metric": "fouls",
                        "priority_score": 88.8,
                        "own": {"n": 8, "mean": 15.1, "stdev": 3.0, "cv": 0.2},
                        "opponent_conceded": {"n": 9, "mean": 13.4, "stdev": 2.1, "cv": 0.16},
                    },
                ]
            }
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                pre_match=pre_match,
                tactical_fx=tactical_fx,
                external_reports=[{"source": "perplexity", "raw_text": rep_md}],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            stats = res["scout_input"]["statistical_context"]
            self.assertTrue(stats["any_detailed_stats_available"])
            self.assertIn("AVAILABLE_IN_TACTICAL_CACHE", stats["metric_provenance"]["home"]["corners"])
            self.assertIn("6.4", res["prompt_markdown"])
            self.assertIn("15.1", res["prompt_markdown"])
            self.assertNotIn("priority_score", res["prompt_markdown"])

    def test_12_false_positive_contradiction_prevented_for_replacement_cell(self):
        with tempfile.TemporaryDirectory() as td:
            rep = (
                "- fixture_id: 1492457\n- event_id: 7901446\n- analysis_cutoff_utc: 2026-11-19T22:00:00Z\n"
                "| Jugador | Estado publicado | Rol y consecuencia |\n"
                "|---|---|---|\n"
                "| Bobadilla | Fuera tras participar con Paraguay | Pablo Maia y Djhordney aparecen como pareja probable |\n"
            )
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                external_reports=[{"source": "perplexity", "raw_text": rep}],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            contras = res["scout_input"]["external_reports"]["contradictions"]
            self.assertFalse(any(c["player"] == "bobadilla" for c in contras))

    def test_13_real_cross_source_contradiction_has_structured_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            rep_a = (
                "- fixture_id: 1492457\n- event_id: 7901446\n- analysis_cutoff_utc: 2026-11-19T21:00:00Z\n"
                "| Jugador | Estado |\n|---|---|\n| Canobbio | Ausencia por control de carga |\n"
            )
            rep_b = (
                "- fixture_id: 1492457\n- event_id: 7901446\n- analysis_cutoff_utc: 2026-11-19T22:15:00Z\n"
                "| Jugador | Estado |\n|---|---|\n| Canobbio | Disponible y probable titular |\n"
            )
            res = prepare_early_scout_package(
                identity=_base_identity(),
                output_dir=Path(td),
                external_reports=[
                    {"source": "ge", "raw_text": rep_a},
                    {"source": "itatiaia", "raw_text": rep_b},
                ],
                asof_utc=datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc),
            )
            contras = res["scout_input"]["external_reports"]["contradictions"]
            self.assertEqual(len(contras), 1)
            c = contras[0]
            self.assertEqual(c["player"], "canobbio")
            self.assertEqual(c["source_a"], "ge")
            self.assertEqual(c["source_b"], "itatiaia")
            self.assertEqual(c["resolution_status"], "PENDIENTE_DE_RESOLUCION")
            self.assertIsNotNone(c["date_a"])
            self.assertIsNotNone(c["date_b"])


if __name__ == "__main__":
    unittest.main()
