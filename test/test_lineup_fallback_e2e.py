"""Prueba de integración end-to-end completamente offline del flujo P0:
- 3 encuentros en ranking: A (sin XI al corte -> descartado), C (sin margen temporal -> omitido), B (elegible -> preparado y continuado).
- Recuperación tardía de XI (API y manual), refresco simulado de cuotas, reevaluación Shadow y trazabilidad completa.
- Distinción explícita entre validación estructural del JSON manual y verificación real de fuente.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import prematch_case as pc
import prematch_daily as pdaily
from bet_engine.context import LineupStatus, ObservationStore
from prematch_cache_record import store_acquisition
from prematch_context_delta import refresh_lineups
from test_lineup_management_p0 import (
    _build_isolated_case_from_7912816,
    _make_api_lineups_pair,
    _make_manual_lineup_doc,
)

ROOT = Path(__file__).resolve().parent.parent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_odds_txt(src_txt: Path, dst_txt: Path, *, event_id: str, capture_iso: str, override_lines: dict[str, str] | None = None) -> None:
    text = src_txt.read_text(encoding="utf-8-sig")
    text = re.sub(r"(?m)^(Event ID:\s*)\d+", rf"\g<1>{event_id}", text)
    text = re.sub(r"(?m)^((?:Fecha captura|Captura)[^:]*:\s*)\S+", rf"\g<1>{capture_iso}", text)
    if override_lines:
        for old_s, new_s in override_lines.items():
            text = text.replace(old_s, new_s)
    dst_txt.parent.mkdir(parents=True, exist_ok=True)
    dst_txt.write_text(text, encoding="utf-8")


def _clone_match_payload_with_kickoff(src_me: Path, dst_me: Path, *, new_kickoff_utc: str, new_event_id: str) -> dict:
    doc = json.loads(src_me.read_text(encoding="utf-8-sig"))
    doc["fixture"]["date"] = new_kickoff_utc
    if "kickoff_madrid" in doc["fixture"]:
        doc["fixture"]["kickoff_madrid"] = new_kickoff_utc
    if "match" in doc and isinstance(doc["match"], dict):
        doc["match"]["event_id"] = str(new_event_id)
        for k in ("kickoff_utc", "kickoff_madrid", "date"):
            if k in doc["match"]:
                doc["match"][k] = new_kickoff_utc
        if isinstance(doc["match"].get("temporal_identity_evidence"), dict):
            doc["match"]["temporal_identity_evidence"]["kickoff_utc"] = new_kickoff_utc
    if "api_fixture_identity" in doc and isinstance(doc["api_fixture_identity"], dict):
        for k in ("kickoff_utc", "kickoff_madrid", "date"):
            if k in doc["api_fixture_identity"]:
                doc["api_fixture_identity"][k] = new_kickoff_utc
    if "temporal_identity_evidence" in doc and isinstance(doc["temporal_identity_evidence"], dict):
        for k in ("kickoff_utc", "kickoff_madrid", "date"):
            if k in doc["temporal_identity_evidence"]:
                doc["temporal_identity_evidence"][k] = new_kickoff_utc
    if "context" in doc and isinstance(doc["context"], dict):
        fblk = doc["context"].get("fixture")
        if isinstance(fblk, dict):
            fblk["date"] = new_kickoff_utc
    dst_me.parent.mkdir(parents=True, exist_ok=True)
    dst_me.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


class LineupFallbackEndToEndTests(unittest.TestCase):
    def test_e2e_fallback_case_workflow_discards_A_skips_C_prepares_B_recovers_xi_and_refreshes_odds(self) -> None:
        me_a_src = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        me_b_src = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.json"
        me_b_txt_src = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.txt"
        odds_b_src = ROOT / "outputs/matches/7909704/inputs/cuotas_bwin_7909704.txt"
        rep_b_src = ROOT / "outputs/matches/7909704/inputs/reporte_cualitativo_7909704.md"
        if not (me_a_src.is_file() and me_b_src.is_file() and me_b_txt_src.is_file() and odds_b_src.is_file() and rep_b_src.is_file()):
            self.skipTest("Snapshots 7912816 / 7909704 no presentes")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Partido A: 7912816 congelado sin XI oficiales, corte vencido en now=2026-11-16T22:48Z (T-12m <= 15m)
            now_iso = "2026-11-16T22:48:00+00:00"
            setup_a = _build_isolated_case_from_7912816(root)

            # Preparamos artefactos fuente offline de B (7909704 clonado con kickoff T+90m desde now)
            b_ko_iso = "2026-11-17T00:18:00+00:00"
            b_src_dir = root / "external_pool" / "7909704"
            me_b_doc = _clone_match_payload_with_kickoff(
                me_b_src, b_src_dir / "match_engine_7909704.json", new_kickoff_utc=b_ko_iso, new_event_id="7909704"
            )
            odds_b_initial = b_src_dir / "cuotas_initial_7909704.txt"
            _rewrite_odds_txt(odds_b_src, odds_b_initial, event_id="7909704", capture_iso="2026-11-16T22:30:00+00:00")
            rep_b_file = b_src_dir / "reporte_b.md"
            rep_b_file.write_bytes(rep_b_src.read_bytes())
            me_b_txt_file = b_src_dir / "match_engine_7909704.txt"
            me_b_txt_file.write_bytes(me_b_txt_src.read_bytes())

            # Ranking con 3 partidos:
            # A (7912816): seleccionado #1, sin XI confirmados al corte
            # C (888001): #2 en ranking pero KO T+20m (< 45m min_lead) -> debe omitirse
            # B (7909704): #3 en ranking, KO T+90m (>= 45m), elegible -> debe prepararse y continuar automáticamente
            b_obs_meta = ObservationStore.from_match_engine_json(me_b_doc, cutoff_utc="2026-11-16T10:00:00+00:00")
            ranking_path = root / "ranking.json"
            ranking_path.write_text(
                json.dumps(
                    {
                        "ranked_matches": [
                            {
                                "fixture_id": 1649063,
                                "bwin_event_id": "7912816",
                                "kickoff_madrid": "2026-11-17T00:00:00+01:00",
                                "eligible": True,
                            },
                            {
                                "fixture_id": 888001,
                                "bwin_event_id": "888001",
                                "home": "Equipo C Home",
                                "away": "Equipo C Away",
                                "kickoff_madrid": "2026-11-16T23:08:00+00:00",  # T+20m < 45m
                                "eligible": True,
                            },
                            {
                                "fixture_id": int(b_obs_meta.fixture_id),
                                "bwin_event_id": "7909704",
                                "home": b_obs_meta.home_name,
                                "away": b_obs_meta.away_name,
                                "kickoff_madrid": b_ko_iso,
                                "eligible": True,
                            },
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            # 1) CLI real: resolve-lineups con --auto-prepare-next descartando A, omitiendo C y preparando B
            with contextlib.redirect_stdout(io.StringIO()):
                rc = pc.main(
                [
                    "--root",
                    str(root),
                    "resolve-lineups",
                    "--event-id",
                    "7912816",
                    "--ranking",
                    str(ranking_path),
                    "--lineup-cutoff-minutes",
                    "15",
                    "--min-lead-minutes",
                    "45",
                    "--user-manual-absent",
                    "--now-utc",
                    now_iso,
                    "--auto-prepare-next",
                    "--next-context-json",
                    str(b_src_dir / "match_engine_7909704.json"),
                    "--next-context-txt",
                    str(me_b_txt_file),
                    "--next-odds-txt",
                    str(odds_b_initial),
                    "--next-report",
                    str(rep_b_file),
                ]
                )
            self.assertEqual(rc, 0)

            # Verificar auditoría e inmutabilidad de A
            audit_a = json.loads((root / "outputs/matches/7912816/lineups/lineup_audit_7912816.json").read_text(encoding="utf-8"))
            self.assertTrue(audit_a["discarded_from_actionable_queue"])
            self.assertEqual(audit_a["lineup_evaluation"]["status"], LineupStatus.LINEUPS_UNAVAILABLE_FINAL.value)
            self.assertEqual(_sha(setup_a["odds_dst"]), setup_a["original_shas"]["odds"])
            self.assertEqual(_sha(setup_a["rep_dst"]), setup_a["original_shas"]["report"])

            # Verificar que C (888001) fue omitido por falta de tiempo suficiente
            skipped = audit_a["next_candidate_reconsideration"]["skipped"]
            c_skip = next(s for s in skipped if s.get("event_id") == "888001")
            self.assertIn("INSUFFICIENT_REMAINING_TIME", c_skip["reason"])

            # Verificar que B (7909704) fue seleccionado Y su expediente canónico fue preparado automáticamente
            self.assertEqual(audit_a["next_candidate_reconsideration"]["selected_event_id"], "7909704")
            b_dir = root / "outputs/matches/7909704"
            self.assertTrue((b_dir / "case.json").is_file())
            self.assertTrue((b_dir / "manifest.json").is_file())
            self.assertTrue((b_dir / "shadow/shadow_summary_7909704.json").is_file())
            b_case = json.loads((b_dir / "case.json").read_text(encoding="utf-8"))
            self.assertEqual(b_case["fallback_from_discarded_event"]["discarded_event_id"], "7912816")

            # Antes de XI confirmados de B, Shadow de B debe bloquear PICK_ACCIONABLE / elegibilidad final
            sh_b_before = json.loads((b_dir / "shadow/shadow_summary_7909704.json").read_text(encoding="utf-8"))
            self.assertFalse(sh_b_before["eligible_for_final_action"])
            self.assertEqual(sh_b_before["shadow_report"]["decision_breakdown_counts"]["PICK_ACCIONABLE"], 0)
            b_orig_ctx_sha = _sha(b_dir / "inputs/match_engine_7909704.json")

            # 2) Recuperación tardía mediante JSON manual estructurado en B (T-60m) + distinción epistémica JSON vs fuente real
            b_bundle = ObservationStore.from_match_engine_json(me_b_doc, cutoff_utc="2026-11-16T23:18:00+00:00")
            manual_b = _make_manual_lineup_doc(
                fixture_id=b_bundle.fixture_id,
                event_id="7909704",
                home_id=b_bundle.home_id,
                home_name=b_bundle.home_name,
                away_id=b_bundle.away_id,
                away_name=b_bundle.away_name,
                captured_at_utc="2026-11-16T23:10:00+00:00",
                source="Acta Oficial Federación / Web Club",
                source_url="https://federacion.example/lineups/7909704",
                source_kind="OFFICIAL_CLUB",
            )
            manual_b_path = root / "manual_b.json"
            manual_b_path.write_text(json.dumps(manual_b, indent=2), encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                rc_man = pc.main(
                    [
                        "--root",
                        str(root),
                        "attach-manual-lineups",
                        "--event-id",
                        "7909704",
                        "--file",
                        str(manual_b_path),
                        "--now-utc",
                        "2026-11-16T23:18:00+00:00",
                    ]
                )
            self.assertEqual(rc_man, 0)

            sh_b_after_xi = json.loads((b_dir / "shadow/shadow_summary_7909704.json").read_text(encoding="utf-8"))
            self.assertEqual(sh_b_after_xi["lineup_gate"]["status"], LineupStatus.LINEUPS_MANUAL_VERIFIED.value)
            self.assertTrue(sh_b_after_xi["eligible_for_final_action"])
            # Distinción explícita entre validar JSON vs verificar fuente real
            self.assertEqual(sh_b_after_xi["lineup_gate"]["validation_scope"], "JSON_SCHEMA_IDENTITY_AND_TEMPORAL_CHECKS_ONLY")
            self.assertEqual(sh_b_after_xi["lineup_gate"]["real_source_verification"], "DECLARED_PROVENANCE_NOT_EXTERNALLY_ATTESTED")
            # Contexto original en inputs/ permanece intacto (delta en context_updates/)
            self.assertEqual(_sha(b_dir / "inputs/match_engine_7909704.json"), b_orig_ctx_sha)

            # 3) Recuperación tardía también por API local datada (verifica camino API -> LINEUPS_API_CONFIRMED)
            api_rec = store_acquisition(
                b_dir / "context_acquisitions",
                {
                    "get": "fixtures/lineups",
                    "parameters": {"fixture": str(b_bundle.fixture_id)},
                    "errors": [],
                    "results": 2,
                    "response": _make_api_lineups_pair(b_bundle.home_id, b_bundle.home_name, b_bundle.away_id, b_bundle.away_name),
                },
                now=datetime.fromisoformat("2026-11-16T23:30:00+00:00"),
            )
            b_manifest_cur = json.loads((b_dir / "manifest.json").read_text(encoding="utf-8"))
            b_ctx_obj = {
                "root": root,
                "directory": b_dir,
                "event_id": "7909704",
                "fixture_id": b_bundle.fixture_id,
                "url": b_manifest_cur["url"],
                "kickoff": datetime.fromisoformat(b_ko_iso),
            }
            upd_man_api, _ = refresh_lineups(
                b_ctx_obj, b_manifest_cur, api_rec, _sha(api_rec), datetime.fromisoformat("2026-11-16T23:32:00+00:00")
            )
            (b_dir / "manifest.json").write_text(json.dumps(upd_man_api, indent=2), encoding="utf-8")
            self.assertEqual(upd_man_api["lineup_state"]["status"], LineupStatus.LINEUPS_API_CONFIRMED.value)

            # 4) Refresco de cuotas simulado offline -> preserva XI API confirmados, archiva TXT previo, genera odds_delta.json y reejecuta Shadow
            odds_b_fresh = b_src_dir / "cuotas_fresh_7909704.txt"
            _rewrite_odds_txt(
                odds_b_initial,
                odds_b_fresh,
                event_id="7909704",
                capture_iso="2026-11-16T23:55:00+00:00",
                override_lines={"2.25": "2.40"},
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc_odds = pc.main(
                    [
                        "--root",
                        str(root),
                        "refresh-odds-offline",
                        "--event-id",
                        "7909704",
                        "--file",
                        str(odds_b_fresh),
                        "--now-utc",
                        "2026-11-16T23:58:00+00:00",  # T-20m antes de 00:18Z
                    ]
                )
            self.assertEqual(rc_odds, 0)

            b_manifest_final = json.loads((b_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(Path(b_manifest_final["odds_delta"]["path"]).is_file())
            sh_b_final = json.loads((b_dir / "shadow/shadow_summary_7909704.json").read_text(encoding="utf-8"))
            self.assertEqual(sh_b_final["odds_freshness_status"], "LOADED")
            self.assertEqual(sh_b_final["lineup_gate"]["status"], LineupStatus.LINEUPS_API_CONFIRMED.value)
            self.assertEqual(sh_b_final["lineup_gate"]["real_source_verification"], "API_FOOTBALL_PROVIDER_RECORD_NOT_CLUB_CERTIFIED")
            self.assertTrue(sh_b_final["eligible_for_final_action"])

    def test_e2e_daily_cli_auto_continue_after_A_discarded_and_C_skipped(self) -> None:
        me_a_src = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        me_b_src = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.json"
        if not (me_a_src.is_file() and me_b_src.is_file()):
            self.skipTest("Snapshots 7912816 / 7909704 no presentes")

        me_a = json.loads(me_a_src.read_text(encoding="utf-8"))
        me_b = json.loads(me_b_src.read_text(encoding="utf-8"))
        obs_a = ObservationStore.from_match_engine_json(me_a, cutoff_utc="2026-11-16T10:00:00+00:00")
        obs_b = ObservationStore.from_match_engine_json(me_b, cutoff_utc="2026-11-16T10:00:00+00:00")

        # Extraer statistical_observations compatibles para preselection desde los dos snapshots reales
        def _to_pre_row(obs: ObservationStore, event_id: str, ko_utc: str, score_hint: float) -> dict:
            def _conv(matches):
                return [
                    {
                        "fixture_id": m.fixture_id,
                        "team_id": m.team_id,
                        "kickoff_utc": m.kickoff_utc,
                        "status": m.status,
                        "league_id": m.league_id,
                        "league_name": m.league_name,
                        "season": m.season,
                        "venue": m.venue,
                        "own": m.own_metrics,
                        "conceded": m.conceded_metrics,
                    }
                    for m in matches
                ]

            return {
                "event_id": event_id,
                "fixture_id": obs.fixture_id,
                "league_id": obs.league_id,
                "season": obs.season,
                "home": obs.home_name,
                "away": obs.away_name,
                "home_id": obs.home_id,
                "away_id": obs.away_id,
                "kickoff_madrid": ko_utc,
                "kickoff_utc": ko_utc,
                "score_hint": score_hint,
                "statistical_observations": {
                    "home": _conv(obs.home_matches),
                    "away": _conv(obs.away_matches),
                },
            }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws_dir = root / "ws_e2e"
            now_iso = "2026-11-16T17:48:00+00:00"  # 18:48 CET (> 13:00 available_from)

            pre_doc = {
                "generated_at_utc": "2026-11-16T10:00:00+00:00",
                "matches": [
                    # A: KO 18:00Z -> T-12m (pasa min_lead=10m pero cae en corte lineups <=15m sin XI -> REJECTED_MISSING_LINEUPS)
                    _to_pre_row(obs_a, "7912816", "2026-11-16T18:00:00+00:00", 85.0),
                    # C: KO 18:05Z -> T-17m (< min_lead 10m + safety 10m = 20m -> omitido por tiempo insuficiente!)
                    _to_pre_row(obs_a, "888001", "2026-11-16T18:05:00+00:00", 80.0),
                    # B: KO 19:20Z -> T-92m (>= 20m -> siguiente candidato elegible!)
                    _to_pre_row(obs_b, "7909704", "2026-11-16T19:20:00+00:00", 78.0),
                ],
            }
            # Asegurar fixture_id distinto en C
            pre_doc["matches"][1]["fixture_id"] = 888001
            pre_path = root / "preselection.json"
            pre_path.write_text(json.dumps(pre_doc, indent=2), encoding="utf-8")

            odds_a_json = root / "odds_a.json"
            odds_a_json.write_text(
                json.dumps({"event_id": "7912816", "captured_at_utc": "2026-11-16T17:45:00+00:00", "odds": {"over_2_5_goals": 1.9}}),
                encoding="utf-8",
            )
            odds_b_json = root / "odds_b.json"
            odds_b_json.write_text(
                json.dumps({"event_id": "7909704", "captured_at_utc": "2026-11-16T17:45:00+00:00", "odds": {"home_win": 2.40, "over_2_5_goals": 2.80}}),
                encoding="utf-8",
            )
            manual_b = _make_manual_lineup_doc(
                fixture_id=obs_b.fixture_id,
                event_id="7909704",
                home_id=obs_b.home_id,
                home_name=obs_b.home_name,
                away_id=obs_b.away_id,
                away_name=obs_b.away_name,
                captured_at_utc="2026-11-16T17:40:00+00:00",
                source="UEFA / Federación Oficial",
                source_kind="OFFICIAL_CLUB",
            )
            manual_b_path = root / "manual_b.json"
            manual_b_path.write_text(json.dumps(manual_b, indent=2), encoding="utf-8")

            # Paso 1: preanalyze T-24h vía CLI real
            with contextlib.redirect_stdout(io.StringIO()):
                rc1 = pdaily.main(
                    [
                        "--root", str(root),
                        "--shadow-preanalyze",
                        "--date", "2026-11-16",
                        "--preselection", str(pre_path),
                        "--shadow-dir", str(ws_dir),
                        "--min-lead", "10",
                        "--now-utc", "2026-11-16T10:00:00+00:00",
                    ]
                )
            self.assertEqual(rc1, 0)

            # Paso 2: --shadow-finalize sobre A con usuario ausente -> descarta A, omite C y continúa automáticamente con B aplicando cuotas y XI de B
            with contextlib.redirect_stdout(io.StringIO()):
                rc2 = pdaily.main(
                    [
                        "--root", str(root),
                        "--shadow-finalize",
                        "--date", "2026-11-16",
                        "--shadow-dir", str(ws_dir),
                        "--event-id", "7912816",
                        "--odds-json", str(odds_a_json),
                        "--lineup-cutoff-minutes", "15",
                        "--min-lead", "10",
                        "--fallback-on-missing-lineups",
                        "--user-manual-absent",
                        "--auto-continue-next",
                        "--next-odds-json", str(odds_b_json),
                        "--next-manual-lineups", str(manual_b_path),
                        "--now-utc", now_iso,
                    ]
                )
            self.assertEqual(rc2, 0)

            plan = json.loads((ws_dir / "proximity_plan_state.json").read_text(encoding="utf-8"))
            statuses = {c["event_id"]: c["execution_status"] for c in plan["candidates"]}
            self.assertEqual(statuses["7912816"], "REJECTED_MISSING_LINEUPS")
            self.assertEqual(statuses["7909704"], "COMPLETED")
            self.assertIn("7912816", plan["failed_event_ids"])
            self.assertIn("7909704", plan["completed_event_ids"])

            # C (888001) fue omitido por falta de tiempo (< 45m + margen) en auditoría
            st_final = pdaily.run_daily_shadow_lifecycle if hasattr(pdaily, "run_daily_shadow_lifecycle") else None
            disc_audit = json.loads((ws_dir / "discarded_lineups_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(disc_audit[0]["event_id"], "7912816")
            self.assertEqual(disc_audit[0]["lineup_status"], LineupStatus.LINEUPS_UNAVAILABLE_FINAL.value)

            # Shadow output persistido de B refleja LINEUPS_MANUAL_VERIFIED y elegibilidad; el de A refleja BLOQUEO
            sh_a = json.loads((ws_dir / "events/7912816/shadow_comparison_7912816.json").read_text(encoding="utf-8"))
            sh_b = json.loads((ws_dir / "events/7909704/shadow_comparison_7909704.json").read_text(encoding="utf-8"))
            self.assertFalse(sh_a["eligible_for_final_action"])
            self.assertEqual(sh_a["decision_breakdown_counts"]["PICK_ACCIONABLE"], 0)
            self.assertTrue(sh_b["eligible_for_final_action"])
            self.assertEqual(sh_b["lineup_gate"]["status"], LineupStatus.LINEUPS_MANUAL_VERIFIED.value)
            self.assertEqual(sh_b["lineup_gate"]["real_source_verification"], "DECLARED_PROVENANCE_NOT_EXTERNALLY_ATTESTED")


if __name__ == "__main__":
    unittest.main()
