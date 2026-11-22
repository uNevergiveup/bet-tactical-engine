from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import argparse
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import prematch_hour_batch as batch
import prematch_steps as steps
from test_prematch_steps import _make_fixture, _make_preselection_match


class HourBatchTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.run = self.root / "run"
        self.folder = self.run / "hour_batches/2026-11-22_1400"
        self.fid, self.eid = 1570455, "7911859"
        self.now = datetime.fromisoformat("2026-11-22T10:00:00+01:00")
        parse_time = steps._now_utc
        clock = patch.object(steps, "_now_utc", side_effect=lambda value=None: parse_time(value) if value else self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.fixtures = [
            self.fixture(self.fid, 14, 0, "Barcelona", "Villarreal", 529, 533),
            self.fixture(202, 14, 35, "Betis", "Getafe", 543, 546),
            self.fixture(303, 15, 0, "Real Madrid", "Osasuna", 541, 727),
            self.fixture(404, 14, 10, "Valencia", "Espanyol", 532, 540),
        ]
        steps._atomic_json(self.run / "fixtures.json", {"window": {
            "start_madrid": "2026-11-22T14:00:00 Europe/Madrid (hora civil)",
            "end_madrid": "2026-11-23T03:00:00 Europe/Madrid (hora civil)",
        }})
        steps._atomic_json(self.run / "fixtures_major.json", {
            "schema_version": steps.fml.VERSION, "fixtures": self.fixtures,
        })
        self.events = {self.fid: self.eid, 202: "7900202", 303: "7900303"}
        steps._atomic_json(self.run / "prospective_trial/bwin/bwin_discovery.json", {"matches": [
            {"fixture_id": fid, "bwin_event_id": eid, "bwin_url": f"https://www.bwin.es/es/sports/eventos/test:{eid}",
             "status": "IDENTITY_VERIFIED_AUTOMATICALLY"}
            for fid, eid in self.events.items()
        ]})
        history_path = self.run / "prospective_trial/enriched/preselection.json"
        self.rank_path = self.run / "prospective_trial/tactical/tactical_signal_engine.json"
        steps._atomic_json(history_path, {
            "generated_at_utc": self.now.isoformat(),
            "matches": [_make_preselection_match(row) for row in self.fixtures],
        })
        steps._atomic_json(self.rank_path, {
            "sources": {"preselection_sha256": steps._sha256_file(history_path)},
            "asof_madrid": self.now.isoformat(),
            "statistical_ranking": [{"fixture_id": self.fid, "position": 54}, {"fixture_id": 202, "position": 1}],
            "fixtures": {str(self.fid): {"signals": [{
                "metric": "goals", "team_side": "home", "status": "SUPPORTED_DESCRIPTIVE_SIGNAL",
                "priority_score": 70, "own": {"mean": 2, "n": 6}, "opponent_conceded": {"mean": 1, "n": 6},
            }]}},
        })

    def fixture(self, fid, hour, minute, home, away, home_id, away_id):
        kickoff = self.now.replace(hour=hour, minute=minute)
        return _make_fixture(fid, home=home, away=away, home_id=home_id, away_id=away_id,
                             league="La Liga", league_id=140, country="Spain",
                             kickoff_utc=kickoff.astimezone(steps.timezone.utc).isoformat(),
                             kickoff_madrid=kickoff.isoformat())

    def cli(self, command, *args, expected=0, hour="14:00"):
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = steps.main([command, "--root", str(self.root), "--eval-dir", str(self.run),
                               "--hour", hour, *args])
        self.assertEqual(code, expected, out.getvalue())
        return out.getvalue()

    def local_capture(self, target, *, price="2.10", line="2,5", extra="", min_lead=45):
        source = self.root / f"odds_{target['fixture_id']}.txt"
        source.write_text(
            f"Event ID: {target['bwin_event_id']}\nCaptura: {self.now.isoformat()}\n\n"
            f"MERCADO: Total de goles | Tiempo reglamentario\nMás de {line} : {price}\n" + extra,
            encoding="utf-8",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(steps.main(["odds-capture", "--root", str(self.root), "--eval-dir", str(self.run),
                                         "--fixture-id", str(target["fixture_id"]), "--snapshot-file", str(source),
                                         "--min-lead", str(min_lead)]), 0)
        return steps._resolve_odds_snapshot(self.run, target, self.now, required=True)

    def packet(self):
        path = next(self.folder.glob("packets/*/lote.json"))
        return path.parent, steps._read_json(path)

    def test_players_join_exact_ft_market_and_export_exploratory_sample(self):
        pre_path = self.run / "prospective_trial/enriched/preselection.json"
        pre = steps._read_json(pre_path)
        match = next(row for row in pre["matches"] if row["fixture_id"] == self.fid)
        response = []
        for row, shots in zip(match["statistical_observations"]["home"], [2, 3, 0, None, 5]):
            row.update(status="FT", team_id=529, opponent_id=900)
            response.append({
                "fixture": {"id": row["fixture_id"], "date": row["kickoff_utc"], "status": {"short": "FT"}},
                "teams": {"home": {"id": 529}, "away": {"id": 900}},
                "league": {"id": 140, "season": 2026},
                "players": [{"team": {"id": 529}, "players": [{
                    "player": {"id": 501, "name": "Álex Ejemplo"},
                    "statistics": [{"games": {"minutes": 40, "substitute": False, "position": "F"},
                                    "shots": {"total": shots, "on": None}, "cards": {"yellow": 1}}],
                }]}],
            })
        cache = self.root / "detail.json"
        steps._atomic_json(cache, {"endpoint": "fixtures", "retrieved_at_utc": self.now.isoformat(), "response": response[:4]})
        future = self.root / "future_detail.json"
        steps._atomic_json(future, {"endpoint": "fixtures", "retrieved_at_utc": self.now.replace(hour=11).isoformat(),
                                    "response": response[4:]})
        pre["api_request_ledger"] = [{"endpoint": "fixtures", "cache_path": str(path), "cache_sha256": steps._sha256_file(path)}
                                     for path in (cache, future)]
        steps._atomic_json(pre_path, pre)
        rank = steps._read_json(self.rank_path)
        rank["sources"]["preselection_sha256"] = steps._sha256_file(pre_path)
        steps._atomic_json(self.rank_path, rank)
        offers = (
            "\nMERCADO: Jugador - Total de tiros (2+)\nAlex Ejemplo : 2.10\nOtro Futbolista : 2.30\n"
            "\nMERCADO: Jugador - Total de tiros a portería (2+)\nAlex Ejemplo : 2.60\n"
            "\nMERCADO: Jugador - Total de tiros (2+) | 1ª parte\nAlex Ejemplo : 2.80\n"
        )
        with patch.object(batch, "_capture", side_effect=lambda _, root, run, target: self.local_capture(target, extra=offers)):
            self.cli("hour-batch-prepare", "--fixture-id", str(self.fid), "--confirm-external", "--export-gemini")
        directory, packet = self.packet()
        match = packet["fixtures"][0]
        [prop] = [option for option in match["priced_and_linked_options"] if option["target_scope"] == "PLAYER"]
        evidence = prop["player_evidence"]
        self.assertEqual((evidence["player_id"], evidence["n_valid"], evidence["hits"], evidence["valid_minutes"]), (501, 3, 2, 120))
        self.assertEqual(evidence["n_missing_metric_in_observed_appearances"], 1)
        self.assertEqual(prop["settlement_status"], "SETTLEMENT_UNVERIFIED")
        self.assertIsNone(prop["accredited_p_model"])
        self.assertEqual([source["path"] for source in match["player_history_sources"]], [str(cache)])
        coverage = match["player_prop_coverage"]["by_family"]
        self.assertEqual(coverage["PLAYER_SHOTS"], {"DESCRIPTIVE_XI_PENDING": 1, "PLAYER_ID_NOT_RESOLVED_FROM_HISTORY": 1,
                                                    "PERIOD_NOT_SUPPORTED": 1})
        self.assertEqual(coverage["PLAYER_ON_TARGET"], {"INSUFFICIENT_OBSERVED_SAMPLE": 1})
        portable = steps._read_json(directory / "mercados_gemini.json")["fixtures"][0]
        self.assertIn(prop, portable["priced_and_linked_options"])
        self.assertNotIn("player_history_sources", portable)

        shortlist = directory / "prefinalistas.json"
        steps._atomic_json(shortlist, {"packet_id": packet["packet_id"], "candidates": [{
            "fixture_id": self.fid, "selection_id": prop["selection_id"], "role": "prefinalist",
            "case_for": "Muestra de tiros al umbral.", "lineup_condition": "Confirmar minutos y rol.",
        }]})
        self.now = self.now.replace(hour=13)
        lineup = {"confirmed_complete_22": True, "refreshed_at_utc": self.now.isoformat(),
                  "lineup_status": "LINEUPS_API_CONFIRMED", "lineup_evaluation": {}, "api_roster": [
                      {"player_id": 999, "name": "Alex Ejemplo", "side": "away", "role": "TITULAR"},
                      {"player_id": 501, "name": "A. Ejemplo", "side": "home", "role": "SUPLENTE"},
                  ]}
        with patch.object(batch, "_refresh_lineup", return_value=lineup), \
                patch.object(batch, "_capture", side_effect=lambda _, root, run, target, **kw: self.local_capture(target, extra=offers)), \
                patch.object(steps, "cmd_model") as model:
            out = self.cli("hour-batch-refresh", "--shortlist", str(shortlist), "--confirm-external")
            self.assertIn("LIQUIDACION_PENDIENTE", out)
            model.assert_not_called()
        closing = steps._read_json(next(directory.glob("refresh/*/cierre.json")))["fixtures"][0]
        [comparison] = closing["comparisons"]
        self.assertEqual(comparison["player_lineup_context"], lineup["api_roster"][1])

    def initial_shortlist(self):
        with patch.object(batch, "_capture", side_effect=lambda _, root, run, target: self.local_capture(target)):
            self.cli("hour-batch-prepare", "--confirm-external")
        directory, packet = self.packet()
        row = next(f for f in packet["fixtures"] if f["fixture_id"] == self.fid)
        option = row["priced_and_linked_options"][0]
        dest = directory / "prefinalistas.json"
        steps._atomic_json(dest, {"packet_id": packet["packet_id"], "candidates": [
            {"fixture_id": self.fid, "selection_id": option["selection_id"], "role": "prefinalist",
             "case_for": "La producción local sostiene la hipótesis.", "lineup_condition": "Confirmar delantero",
             "watch_players": ["101"]},
        ]})
        return directory, dest

    def test_prepare_matches_hour_and_reuses_captures_after_one_fixture_failure(self):
        seen = []

        def capture(_, root, run, target):
            fid = target["fixture_id"]
            seen.append(fid)
            if fid == self.fid and seen.count(fid) == 1:
                raise steps.StepError("BWIN_TEST_RETRY")
            return self.local_capture(target)

        rank = self.rank_path.read_bytes()
        with patch.object(batch, "_capture", side_effect=capture):
            self.cli("hour-batch-prepare", "--confirm-external")
            first_path, first = self.packet()
            self.assertEqual({r["fixture_id"]: r["status"] for r in first["fixtures"]},
                             {self.fid: "ERROR_REINTENTABLE", 202: "REVISABLE"})
            final_stdout = self.cli("hour-batch-prepare", "--confirm-external")
        self.assertEqual(seen, [self.fid, 202, self.fid])
        self.assertEqual(self.rank_path.read_bytes(), rank)
        second = next(p for p in self.folder.glob("packets/*/lote.json") if p.parent != first_path)
        row = next(r for r in steps._read_json(second)["fixtures"] if r["fixture_id"] == self.fid)
        self.assertEqual(row["statistical_position"], 54)
        price = row["priced_and_linked_options"][0]
        self.assertEqual(price["odd"], "2.1")
        self.assertTrue(price["signals"])
        self.assertIsNone(price["accredited_p_model"])
        prompt = second.parent / "encargo_codex_argon.md"
        instructions = prompt.read_text()
        self.assertIn("mejor explicación", instructions)
        for path in (second, row["catalog"], row["snapshot"]["path"],
                     row["statistical_sources"]["preselection"]["path"]):
            self.assertIn(str(path), instructions)
        self.assertIn(f'Lee "{prompt}"', final_stdout)
        self.assertIn(f'guarda el resultado directamente en "{second.parent / "prefinalistas.json"}"', final_stdout)
        instruction_file = prompt.with_name("instruccion_agente_local.txt")
        instruction = instruction_file.read_text().strip()
        self.assertIn(f"archivo_instruccion_agente_local: {instruction_file}", final_stdout)
        self.assertIn(f"instruccion_agente_local: {instruction}", final_stdout)
        self.assertIn(f"## Instrucción para enviar al agente local\n\n```text\n{instruction}", instructions)

    def test_refresh_waits_for_lineups_then_compares_the_same_selection(self):
        directory, shortlist = self.initial_shortlist()
        self.now = self.now.replace(hour=12, minute=55)
        with patch.object(batch, "_fetch_lineups") as fetch, patch.object(batch, "_capture") as capture:
            out = self.cli("hour-batch-refresh", "--shortlist", str(shortlist))
        self.assertIn("XI_PENDIENTE", out)
        fetch.assert_not_called()
        capture.assert_not_called()

        raw = {"response": [
            {"team": {"id": team}, "formation": "4-3-3", "startXI": [
                {"player": {"id": offset + i, "name": f"Jugador {offset + i}"}} for i in range(1, 12)
            ]}
            for team, offset in ((529, 100), (533, 200))
        ], "_acquired_at_utc": self.now.isoformat()}

        def refresh_capture(_, root, run, target):
            self.assertEqual(target["fixture_id"], self.fid)
            return self.local_capture(target, price="1.95")

        with patch.object(batch, "_fetch_lineups", return_value=raw) as fetch, \
                patch.object(batch, "_capture", side_effect=lambda *a, **kw: refresh_capture(*a)) as capture:
            out = self.cli("hour-batch-refresh", "--shortlist", str(shortlist), "--confirm-external")
            self.assertIn("LISTO_REVISION_FINAL", out)
            self.assertIn("instruccion_agente_local", out)
            self.assertIn("resultado_final.md", out)
            self.cli("hour-batch-refresh", "--shortlist", str(shortlist), "--confirm-external")
            fetch.assert_called_once_with(self.fid)
            capture.assert_called_once()
        refreshed = [steps._read_json(p)["fixtures"][0] for p in directory.glob("refresh/*/cierre.json")]
        ready = next(r for r in refreshed if r["status"] == "LISTO_REVISION_FINAL")
        self.assertTrue(ready["lineup"]["confirmed_complete_22"])
        self.assertEqual(ready["comparisons"][0]["price"]["odd_delta"], "-0.15")
        self.assertTrue(Path(ready["model_json"]).is_file())
        self.assertEqual(len(refreshed), 3)
        final_path = next(p for p in directory.glob("refresh/*/revision_final_codex_argon.md")
                          if ready["model_json"] in p.read_text())
        final = final_path.read_text()
        for path in (directory / "lote.json", ready["catalog"], ready["snapshot"]["path"],
                     ready["lineup"]["artifact_path"]):
            self.assertIn(str(path), final)
        instruction = final_path.with_name("instruccion_agente_local.txt").read_text().strip()
        self.assertIn(instruction, final)
        self.assertIn(str(final_path.with_name("resultado_final.md")), instruction)

    def test_early_refresh_reuses_packet_and_limits_shadow_to_independent_selection(self):
        extra = "\nMERCADO: Total de goles | Tiempo reglamentario\nMenos de 2,5 : 1.75\n"
        with patch.object(batch, "_capture", side_effect=lambda _, root, run, target: self.local_capture(target, extra=extra)):
            self.cli("hour-batch-prepare", "--fixture-id", str(self.fid), "--confirm-external")
        directory, packet = self.packet()
        options = packet["fixtures"][0]["priced_and_linked_options"]
        shortlist = directory / "prefinalistas.json"
        candidates = [
            {"fixture_id": self.fid, "selection_id": option["selection_id"], "role": "prefinalist",
             "case_for": "Tesis inicial", "lineup_condition": "Revisar los escenarios de ataque"}
            for option in options
        ]
        self.assertEqual(len(candidates), 2)
        steps._atomic_json(shortlist, {"packet_id": packet["packet_id"], "candidates": candidates})
        self.now = self.now.replace(hour=13)
        command = ("--shortlist", str(shortlist), "--confirm-external", "--lineup-mode", "sin-xi")
        with patch.object(batch, "_fetch_lineups") as fetch, patch.object(batch, "_capture") as capture:
            self.assertIn("NO_CANDIDATES_EXPLICITLY_INDEPENDENT_OF_CONFIRMED_XI", self.cli("hour-batch-refresh", *command))
            fetch.assert_not_called()
            capture.assert_not_called()
        candidates[0]["requires_confirmed_xi"] = False
        steps._atomic_json(shortlist, {"packet_id": packet["packet_id"], "candidates": candidates})
        research_out = self.cli("hour-batch-research", "--shortlist", str(shortlist), "--provider", "gemini",
                                "--research-mode", "deep", "--fixture-id", str(self.fid))
        research_dir = directory / "events" / str(self.fid) / "research"
        response = research_dir / "responses" / f"gemini_{self.eid}_deep.md"
        prompt = research_dir / "requests" / f"encargo_gemini_{self.eid}_deep.md"
        inventory = research_dir / "requests" / f"insumos_gemini_{self.eid}_deep.json"
        self.assertIn(str(response), research_out)
        self.assertIn("alineaciones", prompt.read_text())
        research_input = steps._read_json(inventory)
        self.assertEqual(research_input["fixture"]["fixture_id"], self.fid)
        self.assertEqual(len(research_input["candidates"]), 2)
        response.write_text("# Informe Gemini\n\nHallazgo fechado y fuente.\n", encoding="utf-8")
        self.cli("hour-batch-research", "--shortlist", str(shortlist), "--provider", "gemini",
                 "--research-mode", "deep", "--fixture-id", str(self.fid))
        self.assertIn("Hallazgo fechado", response.read_text())
        with patch.object(batch, "_fetch_lineups") as fetch, \
                patch.object(batch, "_capture_final", side_effect=lambda _, root, run, target: self.local_capture(target, extra=extra)) as capture:
            self.assertIn("LISTO_REVISION_FINAL", self.cli("hour-batch-refresh", *command))
            fetch.assert_not_called()
            capture.assert_called_once()
        closed = [steps._read_json(p) for p in directory.glob("refresh/*/cierre.json")]
        ready = next(result for result in closed if result["fixtures"][0]["status"] == "LISTO_REVISION_FINAL")
        self.assertEqual(ready["lineup_mode"], "sin-xi")
        row = ready["fixtures"][0]
        self.assertIsNone(row["lineup"])
        self.assertEqual(row["pending_lineup_candidates"][0]["selection_id"], candidates[1]["selection_id"])
        [comparison] = row["comparisons"]
        self.assertEqual(comparison["selection_id"], candidates[0]["selection_id"])
        report = steps._read_json(Path(row["model_json"]))
        self.assertFalse(report["lineup_gate"]["confirmed_complete_22"])
        self.assertEqual(report["lineup_gate"]["lineup_independent_selection_keys"], [comparison["canonical_key"]])
        self.assertFalse(any(reason.startswith("LINEUPS_NOT_CONFIRMED") for reason in report["no_bet_reasons"]))
        self.assertEqual({o["selection_key"] for o in report["market_comparisons"] if o["odd"] is not None},
                         {comparison["canonical_key"]})
        final_prompts = [path.read_text() for path in directory.glob("refresh/*/revision_final_codex_argon.md")]
        self.assertTrue(any(str(response) in text for text in final_prompts))

    def test_refresh_marks_removed_line_and_time_close(self):
        directory, shortlist = self.initial_shortlist()
        self.now = self.now.replace(hour=13, minute=0)
        target = self.fixtures[0]
        lineup_path = self.root / f"outputs/matches/{self.eid}/market_phase2/lineup_refresh_{self.eid}.json"
        steps._atomic_json(lineup_path, {
            "eval_dir": str(self.run), "fixture_id": self.fid, "event_id": self.eid,
            "home_id": target["home_id"], "away_id": target["away_id"], "kickoff_utc": target["kickoff_utc"],
            "refreshed_at_utc": self.now.isoformat(), "confirmed_complete_22": True,
            "lineup_status": "LINEUPS_API_CONFIRMED", "lineup_evaluation": {},
        })
        self.now = self.now.replace(minute=5)
        with patch.object(batch, "_capture", side_effect=lambda _, root, run, row, **kw: self.local_capture(row, line="3,5")):
            self.assertIn("CONTRATO_PENDIENTE", self.cli("hour-batch-refresh", "--shortlist", str(shortlist), "--confirm-external"))
        row = steps._read_json(next(directory.glob("refresh/*/cierre.json")))["fixtures"][0]
        price = row["comparisons"][0]["price"]
        self.assertEqual(price["status"], "SELECCION_ORIGINAL_NO_LOCALIZADA")
        self.assertEqual(price["neighboring_lines"][0]["line"], "3.5")
        self.now = self.now.replace(minute=56)
        with patch.object(batch, "_capture") as capture, patch.object(batch, "_fetch_lineups") as fetch:
            self.assertIn("CIERRE_OPERATIVO", self.cli("hour-batch-refresh", "--shortlist", str(shortlist), "--confirm-external"))
            capture.assert_not_called()
            fetch.assert_not_called()

    def test_wrong_hour_and_unknown_selection_are_rejected(self):
        _, shortlist = self.initial_shortlist()
        self.cli("hour-batch-prepare", expected=2, hour="14:30")
        self.cli("hour-batch-refresh", "--shortlist", str(shortlist), expected=2, hour="15:00")
        self.assertIn("NO_SHORTLIST_CANDIDATES_FOR_REQUESTED_FIXTURE",
                      self.cli("hour-batch-refresh", "--shortlist", str(shortlist),
                               "--fixture-id", "999999", expected=2))
        doc = steps._read_json(shortlist)
        doc["candidates"][0]["selection_id"] = "fabricated"
        steps._atomic_json(shortlist, doc)
        self.assertIn("HOUR_CANDIDATE_OUTSIDE_PACKET", self.cli("hour-batch-refresh", "--shortlist", str(shortlist), expected=2))

    def test_night_hour_uses_next_civil_day(self):
        args = steps.build_parser().parse_args([
            "hour-batch-prepare", "--root", str(self.root), "--eval-dir", str(self.run), "--hour", "01:00",
        ])
        _, _, slot, folder = batch._scope(args, steps, self.now)
        self.assertEqual(slot.isoformat(), "2026-11-23T01:00:00+01:00")
        self.assertEqual(folder.name, "2026-11-23_0100")

    def test_refresh_todo_renews_only_legacy_captures_and_exports_complete_catalog(self):
        initial, _ = self.initial_shortlist()
        packet = steps._read_json(initial / "lote.json")
        verified = {row["snapshot"]["path"] for row in packet["fixtures"] if row["fixture_id"] == self.fid}

        def view(_, snapshot):
            return {"status": "TODO_ACTIVO_EN_DOM"} if snapshot and snapshot["path"] in verified else {}

        def capture(_, root, run, target):
            fresh = self.local_capture(target, extra="\nMERCADO: Penalti - Sí/No\nSí : 3.00\n")
            verified.add(fresh["path"])
            return fresh

        with patch.object(batch, "_todo_view", side_effect=view), patch.object(batch, "_capture", side_effect=capture) as scrape:
            out = self.cli("hour-batch-prepare", "--confirm-external", "--refresh-todo", "--export-gemini")
        self.assertEqual([call.args[-1]["fixture_id"] for call in scrape.call_args_list], [202])
        portable = next(self.folder.glob("packets/*/mercados_gemini.json"))
        self.assertNotEqual(portable.parent, initial)
        self.assertIn("inventario_gemini", out)
        export = steps._read_json(portable)
        rows = {row["fixture_id"]: row for row in export["fixtures"]}
        self.assertEqual(len(rows), 2)
        family_column = export["quote_columns"].index("market_family")
        self.assertIn("PENALTY_YES_NO", {q[family_column] for q in rows[202]["quotes"]})
        self.assertNotIn("PENALTY_YES_NO", {q["market_family"] for q in rows[202]["priced_and_linked_options"]})
        self.assertIn("Inventaría todas", (portable.parent / "encargo_gemini.md").read_text())
        self.assertIn("HOUR_BATCH_REFRESH_TODO_REQUIRES_CONFIRM_EXTERNAL",
                      self.cli("hour-batch-prepare", "--refresh-todo", expected=2))

    def test_targeted_packets_keep_joint_cards_selectable_only_for_research(self):
        joint = (
            "\nMERCADO: Ambos equipos recibirán X o más tarjetas\n1+ · Sí : 1.32\n1+ · No : 3.10\n"
            "\nMERCADO: Número de tarjetas en total | Tiempo reglamentario\nMás de 3,5 : 1.73\n"
            "\nMERCADO: Recibe una tarjeta\nJugador de prueba : 2.50\n"
        )
        with patch.object(batch, "_capture", side_effect=lambda _, root, run, target: self.local_capture(target, extra=joint)) as capture:
            self.cli("hour-batch-prepare", "--fixture-id", str(self.fid), "--confirm-external")
        self.assertEqual(capture.call_count, 1)
        directory, packet = self.packet()
        self.assertEqual([f["fixture_id"] for f in packet["fixtures"]], [self.fid])
        fixture = packet["fixtures"][0]
        by_family = {}
        for option in fixture["priced_and_linked_options"]:
            by_family.setdefault(option["market_family"], []).append(option)
        self.assertNotIn("PLAYER_CARDS", by_family)
        self.assertIn("PLAYER_CARDS", steps._read_json(Path(fixture["catalog"]))["counts_by_family"])
        self.assertEqual(len(by_family["CARDS_BOTH_TEAMS"]), 2)
        option = next(row for row in by_family["CARDS_BOTH_TEAMS"] if row["direction"] == "yes")
        self.assertEqual(option["line"], "1")
        self.assertIsNone(option["contract_id"])
        for row in [option, by_family["CARDS_TOTAL"][0]]:
            self.assertEqual(row["settlement_status"], "SETTLEMENT_UNVERIFIED")
            self.assertIsNone(row["accredited_p_model"])
            self.assertEqual(row["card_review"]["status"], "EXPLORATORY_SETTLEMENT_UNVERIFIED")
        shortlist = directory / "prefinalistas.json"
        steps._atomic_json(shortlist, {"packet_id": packet["packet_id"], "candidates": [
            {"fixture_id": self.fid, "selection_id": option["selection_id"], "role": "prefinalist",
             "case_for": "Comparar amarillas de ambos.", "lineup_condition": "Liquidación pendiente."},
        ]})
        self.assertIn("XI_PENDIENTE", self.cli("hour-batch-refresh", "--shortlist", str(shortlist)))
        self.now = self.now.replace(hour=13, minute=0)
        lineup = {"confirmed_complete_22": True, "refreshed_at_utc": self.now.isoformat(),
                  "lineup_status": "LINEUPS_API_CONFIRMED", "lineup_evaluation": {}}
        with patch.object(batch, "_refresh_lineup", return_value=lineup), \
                patch.object(batch, "_capture", side_effect=lambda _, root, run, target, **kw: self.local_capture(target, extra=joint)), \
                patch.object(steps, "cmd_model") as model:
            out = self.cli("hour-batch-refresh", "--shortlist", str(shortlist), "--confirm-external")
            self.assertIn("LIQUIDACION_PENDIENTE", out)
            model.assert_not_called()

    def test_confirmed_lineups_at_t40_allow_final_capture(self):
        directory, shortlist = self.initial_shortlist()
        self.now = self.now.replace(hour=13, minute=20)
        target = self.fixtures[0]
        lineup_path = self.root / f"outputs/matches/{self.eid}/market_phase2/lineup_refresh_{self.eid}.json"
        steps._atomic_json(lineup_path, {
            "eval_dir": str(self.run), "fixture_id": self.fid, "event_id": self.eid,
            "home_id": target["home_id"], "away_id": target["away_id"], "kickoff_utc": target["kickoff_utc"],
            "refreshed_at_utc": self.now.isoformat(), "confirmed_complete_22": True,
            "lineup_status": "LINEUPS_API_CONFIRMED", "lineup_evaluation": {},
        })
        with patch.object(steps, "cmd_odds_capture", side_effect=steps.StepError("CAPTURE_TEST_SENTINEL")) as capture, \
                patch.object(steps, "cmd_model") as model:
            self.assertIn("CAPTURE_TEST_SENTINEL", self.cli("hour-batch-refresh", "--shortlist", str(shortlist), "--confirm-external"))
            capture.assert_called_once()
            self.assertEqual(capture.call_args.args[0].min_lead, 5)
            model.assert_not_called()
        result = steps._read_json(next(directory.glob("refresh/*/cierre.json")))["fixtures"][0]
        self.assertEqual(result["reason"], "CAPTURE_TEST_SENTINEL")
        self.assertNotIn("model_decision", result)

    def test_confirmed_xi_at_t19_reuses_recent_pre_xi_capture(self):
        directory, shortlist = self.initial_shortlist()
        target = steps._bwin_reconcile_records(self.run, steps._read_json(self.run / "fixtures_major.json"), self.now, min_lead_minutes=0)["verified"][0]
        self.now = self.now.replace(hour=13, minute=35)
        captured = self.local_capture(target, price="2.05", min_lead=0)
        lineup_path = self.root / f"outputs/matches/{self.eid}/market_phase2/lineup_refresh_{self.eid}.json"
        self.now = self.now.replace(minute=40)
        steps._atomic_json(lineup_path, {
            "eval_dir": str(self.run), "fixture_id": self.fid, "event_id": self.eid,
            "home_id": target["home_id"], "away_id": target["away_id"], "kickoff_utc": target["kickoff_utc"],
            "refreshed_at_utc": self.now.isoformat(), "confirmed_complete_22": True,
            "lineup_status": "LINEUPS_API_CONFIRMED", "lineup_evaluation": {"status": "LINEUPS_API_CONFIRMED", "confirmed_complete_22": True},
        })
        self.now = self.now.replace(minute=41)
        with patch.object(batch, "_capture") as capture:
            out = self.cli("hour-batch-refresh", "--shortlist", str(shortlist), "--confirm-external")
            capture.assert_not_called()
        self.assertIn("LISTO_REVISION_FINAL", out)
        result = steps._read_json(next(directory.glob("refresh/*/cierre.json")))["fixtures"][0]
        self.assertEqual(result["snapshot"]["sha256"], captured["sha256"])
        self.assertTrue(result["price_captured_before_confirmed_xi"])
        self.assertEqual(result["model_deadline_utc"], "2026-11-22T12:55:00+00:00")

    def test_manual_provider_xi_is_validated_without_api(self):
        target = steps._bwin_reconcile_records(self.run, steps._read_json(self.run / "fixtures_major.json"), self.now, min_lead_minutes=0)["verified"][0]
        self.now = self.now.replace(hour=13, minute=40)
        image = self.root / "confirmed.png"
        image.write_bytes(b"offline screenshot")
        source = self.root / "manual.json"
        steps._atomic_json(source, {
            "fixture_id": self.fid, "event_id": self.eid, "source": "Proveedor de prueba",
            "source_kind": "EXTERNAL_PROVIDER", "confirmed": True,
            "captured_at_utc": self.now.isoformat(),
            "screenshots": [{"path": str(image), "sha256": steps._sha256_file(image)}],
            "teams": {
                side: {"team_id": tid, "team_name": name,
                       "starters": [f"{side} jugador {n}" for n in range(11)]}
                for side, tid, name in (("home", 529, "Barcelona"), ("away", 533, "Villarreal"))
            },
        })
        args = argparse.Namespace(manual_lineups_json=source, confirm_external=False)
        lineup_path = self.root / f"outputs/matches/{self.eid}/market_phase2/lineup_refresh_{self.eid}.json"
        with patch.object(batch, "_fetch_lineups") as fetch:
            result = batch._refresh_lineup(steps, args, self.run, self.root / "output", target, [], self.now, lineup_path)
            fetch.assert_not_called()
        self.assertTrue(result["confirmed_complete_22"])
        self.assertEqual(result["lineup_status"], "LINEUPS_EXTERNAL_CONFIRMED")
        self.assertEqual(len([p for p in result["api_roster"] if p["role"] == "TITULAR"]), 22)
        self.assertEqual(result["source_manual"]["screenshots"][0]["sha256"], steps._sha256_file(image))
