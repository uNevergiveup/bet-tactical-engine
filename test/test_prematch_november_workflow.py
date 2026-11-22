from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import prematch_steps as steps
from test_prematch_steps import _make_fixture, _make_preselection_match


ROOT = Path(__file__).resolve().parents[1]
MORNING = "prueba_bwin_20261121_014321"
EVENING = "prueba_noche_20261121_193430"
MIAMI, BOCA = 1649495, 1647047
MIDDAY = "2026-11-21T16:00:00+01:00"
BEFORE_ODDS = "2026-11-21T22:50:00+01:00"
AFTER_ODDS = "2026-11-21T23:20:00+01:00"
AFTER_KICKOFF = "2026-11-22T00:02:00+01:00"


def original(run):
    return ROOT / "outputs/discovery" / run


class NovemberWorkflowTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run = self.root / "outputs/discovery" / MORNING
        self.dossier = self.root / "outputs/matches/7912617"
        source = {
            "schema_version": "1.0-discovery", "generated_at_utc": "2026-11-21T00:45:41Z",
            "window": {"start_madrid": "2026-11-21T15:00:00+01:00", "end_madrid": "2026-11-22T03:00:00+01:00"},
            "fixtures": [
                _make_fixture(MIAMI, home="Inter Miami", away="Columbus Crew", home_id=9568, away_id=1613,
                              league="Major League Soccer", league_id=253, country="USA",
                              kickoff_utc="2026-11-21T23:00:00+00:00", kickoff_madrid="2026-11-22T00:00:00+01:00"),
                _make_fixture(BOCA, home="Atletico-MG", away="Boca Juniors", home_id=1062, away_id=451,
                              league="CONMEBOL Sudamericana", league_id=11, country="World",
                              kickoff_utc="2026-11-21T22:30:00+00:00", kickoff_madrid="2026-11-21T23:30:00+01:00"),
            ],
        }
        self.discovery = self.run / "fixtures.json"
        steps._atomic_json(self.discovery, source)
        self.major = self.run / "fixtures_major.json"
        self.pre = self.run / "prospective_trial/enriched/preselection.json"
        self.rank = self.run / "prospective_trial/tactical/tactical_signal_engine.json"
        self.bwin = self.run / "prospective_trial/bwin/bwin_discovery.json"
        doc = steps.fml.filter_document(source, self.discovery.read_bytes(), policy=steps.DEFAULT_POLICY)
        steps._atomic_json(self.major, doc)
        self.fixtures = {x["fixture_id"]: x for x in doc["fixtures"]}
        self.verify(MIAMI, "7912617")
        self.write_history()

    def verify(self, fixture, event):
        doc = steps._read_json(self.bwin) if self.bwin.is_file() else {"matches": []}
        doc["matches"].append({
            "fixture_id": fixture, "bwin_event_id": event,
            "bwin_url": f"https://www.bwin.es/es/sports/eventos/prueba:{event}",
            "status": "IDENTITY_VERIFIED_AUTOMATICALLY",
        })
        steps._atomic_json(self.bwin, doc)

    def write_history(self):
        match = self.history_for(MIAMI)
        steps._atomic_json(self.pre, {
            "schema_version": "1.3-preselection", "generated_at_utc": "2026-11-21T13:32:42Z",
            "source_filter_sha256": steps._sha256_file(self.major),
            "source_filter_path": str(self.major), "source_filter_generated_at_utc": "2026-11-21T00:50:00Z",
            "counts": {"evaluated": 1, "fully_evaluated": 1},
            "matches": [match], "methodology": {"sample_history_cap": 20}, "api_errors": {},
            "api_usage": {"requests_sent": 0, "cache_hits": 0},
        })
        steps._atomic_json(self.rank, {
            "asof_madrid": "2026-11-21T14:38:43+01:00",
            "sources": {"preselection_sha256": steps._sha256_file(self.pre)},
            "statistical_ranking": [{"fixture_id": MIAMI, "position": 1, "statistical_research_score": 45.0}],
            "fixtures": {str(MIAMI): {"signals": [
                {"metric": "corners", "team_side": "away", "status": "SUPPORTED_DESCRIPTIVE_SIGNAL",
                 "priority_score": 45, "own": {"mean": 7.1, "n": 8}, "opponent_conceded": {"mean": 6.1, "n": 6}},
            ], "contradictions": ["Caída de remates en la última muestra"]}},
        })

    def history_for(self, fixture_id):
        return {**_make_preselection_match(self.fixtures[fixture_id]), "research_priority_score": 88,
                "preliminary_coverage_score": 80}

    def cli(self, command, *options, asof=AFTER_ODDS, code=0, target=True):
        argv = [command, "--root", str(self.root), "--eval-dir", str(self.run)]
        if asof:
            argv += ["--asof", asof]
        if target:
            argv += ["--fixture-id", str(MIAMI), "--event-id", "7912617"]
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            result = steps.main([*argv, *options])
        self.assertEqual(result, code, out.getvalue())
        return out.getvalue()

    def test_reports_zero_one_several_and_rerun_preserves_evidence(self):
        self.cli("select-fixture")
        inbox = self.dossier / "research"
        grok = inbox / "grok_7912617.md"
        perplexity = inbox / "perplexity_7912617.md"
        gemini = inbox / "gemini_7912617.md"
        marker = gemini.read_bytes()
        perplexity.write_text("")
        self.cli("reasoning-package")
        original_package = self.dossier / "codex_reasoning_package.json"
        saved = original_package.read_bytes()
        none = steps._read_json(original_package)["external_reports"]
        self.assertEqual((len(none["accepted"]), len(none["ignored"]), len(none["rejected"])), (0, 3, 0))

        report_header = "fixture_id: 1649495\nevent_id: 7912617\ngenerated_at_utc: 2026-11-21T22:11:00Z\n"
        grok.write_text(report_header + "\n| John Smith | OUT | [Club](https://club.example/news?utm_source=grok) |\n")
        self.cli("select-fixture")
        self.assertEqual(perplexity.read_text(), "")
        self.assertEqual(gemini.read_bytes(), marker)
        self.assertIn("John Smith", grok.read_text())
        self.cli("early-scout")
        scout = self.dossier / "early_scout/early_scout_input_7912617.json"
        single = steps._read_json(scout)["external_reports"]
        self.assertEqual([item["source"] for item in single["accepted"]], ["grok"])

        perplexity.write_text(report_header + "\n| John Smith | Disponible | [Club](https://club.example/news#top) |\n")
        gemini.write_text(report_header.replace("7912617", "7901187") + "\nHallazgos sobre el encuentro.\n")
        self.cli("reasoning-package")
        revision = next(self.dossier.glob("revisions/*/codex_reasoning_package.json"))
        multiple = steps._read_json(revision)["external_reports"]
        self.assertEqual([item["source"] for item in multiple["accepted"]], ["grok", "perplexity"])
        self.assertEqual(multiple["rejected"][0]["source"], "gemini")
        self.assertEqual(multiple["repeated_sources"][0]["independent_corroborations"], 1)
        self.assertEqual(len(multiple["contradictions"]), 1)
        self.assertEqual(original_package.read_bytes(), saved)

    def test_provider_requests_include_fixture_evidence_and_preserve_inbox(self):
        self.cli("early-scout", "--research-phase", "deep", asof="2026-11-21T19:00:00+01:00")
        self.cli("early-scout", "--research-phase", "update", "--research-provider", "grok")
        inbox = self.dossier / "research"
        deep = next(inbox.glob("requests/deep_gemini_*/encargo_gemini_*.md"))
        update = next(inbox.glob("requests/update_grok_*/encargo_grok_*.md"))
        self.assertIn("producción", deep.read_text())
        self.assertIn("7.1", deep.read_text())
        self.assertIn("Caída de remates", update.read_text())
        self.assertIn("corners", update.read_text())
        self.assertIn("RESEARCH_UPDATE_REQUIRES_ONE_PROVIDER",
                      self.cli("early-scout", "--research-phase", "update", code=2))

    def test_manifest_resolves_real_snapshot(self):
        actual_run = original(MORNING)
        if not (actual_run / "prospective_trial/captures/7912617/capture_manifest.json").is_file():
            self.skipTest("Local November capture unavailable")
        target = steps._resolve_target_rec_from_eval(
            actual_run, steps.argparse.Namespace(fixture_id=MIAMI, event_id="7912617"),
            steps._now_utc(AFTER_ODDS),
        )
        real = steps._resolve_odds_snapshot(actual_run, target, steps._now_utc(AFTER_ODDS), required=True)
        self.assertTrue(real["manifest_hash_verified"])
        self.assertIn("20261121T221211Z", real["path"])
        self.assertEqual(real["captured_at_utc"], "2026-11-21T22:12:28+00:00")

    def test_manifest_chain_and_rejection_of_foreign_or_modified_files(self):
        source = self.root / "capture.txt"
        source.write_text("Event ID: 7912617\nCaptura: 2026-11-21T22:12:28Z\n\n"
                          "MERCADO: Resultado del partido | Tiempo reglamentario\nInter Miami : 1.65\nX : 4.00\nColumbus Crew : 5.00\n")
        captured = self.cli("odds-capture", "--snapshot-file", str(source), asof="2026-11-21T23:14:00+01:00")
        catalog = self.cli("market-catalog")
        self.assertIn(next(line.strip().split("snapshot [1649495/7912617]: ")[1] for line in captured.splitlines() if "snapshot [" in line), catalog)
        self.assertIn("catalog_json", catalog)
        package_run = self.cli("reasoning-package")
        self.assertIn("snapshot", package_run)
        analysis = self.root / "scout.json"
        steps._atomic_json(analysis, {
            "coaches_and_systems": {}, "players": {}, "tactical_matchup": {},
            "competitive_context": {}, "hypotheses": [],
        })
        self.cli("early-scout", "--import-analysis", str(analysis))
        scout_path = self.dossier / "early_scout/early_scout_7912617.json"
        catalog_path = self.dossier / "market_phase2/bwin_catalog_7912617.json"
        self.cli("market-match", "--scout-json", str(scout_path), "--catalog-json", str(catalog_path))
        match_path = self.dossier / "market_phase2/market_match_7912617.json"
        self.cli("market-compare", "--scout-json", str(scout_path), "--catalog-json", str(catalog_path),
                 "--match-json", str(match_path))

        capture_dir = self.run / "prospective_trial/captures/7912617"
        manifest = capture_dir / "capture_manifest.json"
        doc = steps._read_json(manifest)
        row = doc["captures"][0]
        row["snapshot_path"] = str(source)
        steps._atomic_json(manifest, doc)
        self.assertIn("ODDS_SNAPSHOT_OUTSIDE_EVALUATION", self.cli("market-catalog", code=2))
        wrong = capture_dir / "other.txt"
        wrong.write_text("Event ID: 7901187\nCaptura: 2026-11-21T20:00:00Z\n")
        row.update(snapshot_path=str(wrong), snapshot_sha256=steps._sha256_file(wrong))
        steps._atomic_json(manifest, doc)
        self.assertIn("ODDS_SNAPSHOT_EVENT_ID_MISMATCH", self.cli("market-catalog", code=2))
        wrong.write_text("Event ID: 7912617\n")
        self.assertIn("ODDS_SNAPSHOT_HASH_MISMATCH", self.cli("market-catalog", code=2))

    def test_first_run_and_actual_boca_gap_are_diagnosed_without_mutations(self):
        if not all((original(run) / "fixtures_major.json").is_file() for run in (MORNING, EVENING)):
            self.skipTest("Local November runs unavailable")
        now = steps._now_utc(AFTER_ODDS)
        self.assertEqual(steps._first_phase1_run(ROOT, now.astimezone(steps.trial.TZ).date(), now), original(MORNING))
        for run in (MORNING, EVENING):
            output = StringIO()
            with self.subTest(run=run), redirect_stdout(output):
                status = steps.main(["fixture-update", "--eval-dir", str(original(run)), "--fixture-id", str(BOCA),
                                     "--asof", BEFORE_ODDS])
                self.assertEqual(status, 0)
                self.assertIn("Event ID observado=7901187", output.getvalue())
                self.assertIn("ranking: AUSENTE", output.getvalue())
                self.assertIn("primera etapa pendiente: VINCULO_BWIN", output.getvalue())

    def test_selective_reclassification_preserves_link_history_and_rebuilds_ranking(self):
        major_doc = steps._read_json(self.major)
        boca = major_doc["fixtures"].pop(next(i for i, row in enumerate(major_doc["fixtures"]) if row["fixture_id"] == BOCA))
        major_doc["review"].append({**boca, "filter_status": "REVIEW", "filter_reason": "TIER_UNKNOWN"})
        steps._atomic_json(self.major, major_doc)
        self.write_history()
        self.verify(BOCA, "7901187")
        links_before = self.bwin.read_bytes()
        history_before = self.pre.read_bytes()
        cmd = ["--fixture-id", str(BOCA)]
        report = self.cli("fixture-update", *cmd, "--apply", asof=MIDDAY, target=False)
        self.assertIn("TIER_UNKNOWN → ELIGIBLE", report)
        self.assertEqual(self.bwin.read_bytes(), links_before)
        self.assertEqual(self.pre.read_bytes(), history_before)
        self.cli("phase1-list", asof=MIDDAY, target=False)
        phase_list = next(self.run.glob("segunda_eleccion*_replay.md"))
        self.assertIn("ausencia del ranking **1**", phase_list.read_text())

        clock = datetime.fromisoformat(MIDDAY)
        fresh_match = self.history_for(BOCA)

        def evaluate(major, client, now, **options):
            self.assertEqual(options["fixture_ids"], {BOCA})
            return {"schema_version": "1.3-preselection", "generated_at_utc": now.isoformat(),
                    "matches": [fresh_match], "counts": {}, "api_errors": {},
                    "api_usage": {"requests_sent": 0, "cache_hits": 0},
                    "methodology": {"sample_details_cap": 8}}

        parse_clock = steps._now_utc
        with patch.object(steps, "_now_utc", side_effect=lambda value=None: parse_clock(value) if value else clock), \
                patch("config.constants.API_KEY", "offline-test"), patch("preselect_matches.ApiFootball"), \
                patch("preselect_matches.run_evaluator", side_effect=evaluate) as fetch:
            self.cli("fixture-update", *cmd, "--apply", "--confirm-external", asof=None, target=False)
        fetch.assert_called_once()
        merged = steps._read_json(self.pre)
        self.assertEqual({m["fixture_id"] for m in merged["matches"]}, {MIAMI, BOCA})
        self.assertEqual(merged["snapshot_migration"]["retained_fixture_ids"], [MIAMI])
        ranking = steps._read_json(self.rank)["statistical_ranking"]
        self.assertIn(BOCA, {m["fixture_id"] for m in ranking})
        self.assertTrue(list(self.run.glob("ranking_archive/*/tactical_signal_engine.json")))
        self.assertEqual(self.bwin.read_bytes(), links_before)

    def test_close_by_time_records_operation_separately_and_audits_untouched_files(self):
        self.assertIn("MODEL_MIN_LEAD_FLOOR", self.cli("model", "--min-lead", "5", code=2))
        self.assertIn("LEAD_BELOW_MIN_PREMATCH", self.cli("model", asof="2026-11-21T23:35:00+01:00", code=2))
        closed = self.cli("model", asof=AFTER_KICKOFF, code=2)
        self.assertIn("POST_KICKOFF", closed)
        self.assertIn("audit-event", closed)
        for record in (self.dossier / "operations").glob("model_*.json"):
            operation = steps._read_json(record)
            self.assertEqual(operation["status"], "OPERATION_BLOCKED_BY_TIME")
            self.assertIsNone(operation["model_decision"])
        self.assertFalse((self.dossier / "model").exists())
        analysis = self.dossier / "analysis/analisis_7912617.md"
        analysis.parent.mkdir()
        analysis.write_text("Candidatos documentados en esta prueba")
        analysis.with_name("analisis_7912617_auditoria.md").write_text("Candidatos reconstruidos")
        clock = datetime.fromisoformat(AFTER_ODDS)
        parse_clock = steps._now_utc
        with patch.object(steps, "_now_utc", side_effect=lambda value=None: parse_clock(value) if value else clock):
            self.cli("audit-event", asof=None)
        self.cli("audit-event", asof=AFTER_KICKOFF)
        inventories = [steps._read_json(path) for path in (self.dossier / "audit").glob("*.json")]
        audit = next(doc for doc in inventories if doc["mode"] == "RETROSPECTIVE_INVENTORY")
        candidates = {r["candidate_provenance"] for r in audit["files"] if "candidate_provenance" in r}
        self.assertIn("PREFINALISTA_REGISTRADO_PREKICKOFF", candidates)
        self.assertIn("RECONSTRUCCION_O_REVISION_RETROSPECTIVA", candidates)
        self.assertEqual(audit["evidence_cutoff_utc"], audit["kickoff_utc"])
        self.assertEqual(analysis.read_text(), "Candidatos documentados en esta prueba")
