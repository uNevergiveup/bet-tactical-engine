import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import match_identity as mi
import prematch_case as pc
import prematch_context_delta as pcd
import prematch_multi as pm
import validate_prematch as vp
from prematch_cache_record import acquisition_record
from bet_engine.context.lineup_manager import (
    LineupStatus,
    evaluate_fixture_lineup_state,
    extract_fixture_team_identity,
)
from bet_engine.context.observation_store import ObservationStore

ROOT = Path(__file__).resolve().parent.parent
EVENT_7905487 = ROOT / "outputs" / "matches" / "7905487"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ref(path: Path) -> dict[str, str]:
    p = path.resolve()
    return {"path": str(p), "sha256": _sha256(p)}


def _clone_isolated_7905487(tmp_root: Path, *, shift_kickoff_to: str | None = None, report_variant: str = "15bb") -> dict:
    """Clona en directorio temporal aislado los artefactos reales de 7905487 sin tocar originales."""
    src_delta = EVENT_7905487 / "context_updates" / "05239425bf28e513b86bec42"
    src_odds = EVENT_7905487 / "inputs" / "cuotas_bwin_7905487.txt"
    src_rep = (
        EVENT_7905487
        / "research"
        / "qualitative"
        / (
            "0e5aeabe98f547db5b776a91a1d6473b92297d65b1945ed39f1ea5f56269bbcc"
            if report_variant == "0e5a"
            else "15bb26f7db9e3b1ce86ffe40299694b9b5a333d2e1a99eaa761ba88dbc252b3f"
        )
        / "reporte_cualitativo_7905487.md"
    )
    if not (src_delta.is_dir() and src_odds.is_file() and src_rep.is_file()):
        raise FileNotFoundError("Snapshots reales 7905487 no disponibles para la prueba")

    ev_dir = tmp_root / "outputs" / "matches" / "7905487"
    inputs = ev_dir / "inputs"
    sel_dir = ev_dir / "selection"
    (ev_dir / "lineups").mkdir(parents=True, exist_ok=True)
    inputs.mkdir(parents=True, exist_ok=True)
    sel_dir.mkdir(parents=True, exist_ok=True)

    odds_dst = inputs / "cuotas_bwin_7905487.txt"
    shutil.copy2(src_odds, odds_dst)

    ctx_json = json.loads((src_delta / "match_engine_7905487.json").read_text(encoding="utf-8"))
    ctx_txt_raw = (src_delta / "match_engine_7905487.txt").read_text(encoding="utf-8")
    rep_raw = src_rep.read_text(encoding="utf-8")

    if shift_kickoff_to is not None:
        old_utc = "2026-11-17T19:45:00+00:00"
        old_mad = "2026-11-17T20:45:00+01:00"
        new_utc_dt = datetime.fromisoformat(shift_kickoff_to).astimezone(timezone.utc)
        new_utc = new_utc_dt.isoformat()
        from zoneinfo import ZoneInfo

        new_mad = new_utc_dt.astimezone(ZoneInfo("Europe/Madrid")).isoformat()
        ctx_json["fixture"]["date"] = new_utc
        ctx_json["context"]["fixture"]["date"] = new_utc
        ctx_json["match"]["event_date"] = new_mad
        ctx_json["match"]["temporal_identity_evidence"]["kickoff_utc"] = new_utc
        ctx_txt_raw = ctx_txt_raw.replace(old_utc, new_utc)
        rep_raw = rep_raw.replace(old_utc, new_utc).replace(old_mad, new_mad)
    else:
        new_utc = "2026-11-17T19:45:00+00:00"
        from zoneinfo import ZoneInfo

        new_mad = datetime.fromisoformat(new_utc).astimezone(ZoneInfo("Europe/Madrid")).isoformat()

    txt_dst = inputs / "match_engine_7905487.txt"
    json_dst = inputs / "match_engine_7905487.json"
    txt_dst.write_text(ctx_txt_raw, encoding="utf-8")
    json_dst.write_text(json.dumps(ctx_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    id_dst = mi.write_identity(json_dst, inputs / "match_identity.json")

    rep_dst = inputs / "reporte_cualitativo_7905487.md"
    rep_dst.write_text(rep_raw, encoding="utf-8")

    handoff_dst = sel_dir / "v3_handoff.json"
    handoff_dst.write_text(
        json.dumps({"event_id": "7905487", "fixture_id": 1528992}, indent=2) + "\n",
        encoding="utf-8",
    )
    sel_dst = sel_dir / "selected_match.json"
    sel_dst.write_text(
        json.dumps(
            {
                "schema_version": "1.0-selected-single-fixture",
                "status": "SELECTED_FOR_DEEP_RESEARCH",
                "fixture_id": 1528992,
                "bwin_event_id": "7905487",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/gales-portugal-2:7905487",
                "home": "Wales",
                "away": "Portugal",
                "home_api_id": 767,
                "away_api_id": 27,
                "league_id": 5,
                "season": 2026,
                "kickoff_madrid": new_mad,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    case_doc = {
        "schema_version": "2.0-canonical-prematch-case",
        "event_id": "7905487",
        "fixture_id": 1528992,
        "bwin_url": "https://www.bwin.es/es/sports/eventos/gales-portugal-2:7905487",
        "home": {"name": "Wales", "api_team_id": 767},
        "away": {"name": "Portugal", "api_team_id": 27},
        "competition": {"league_id": 5, "name": "UEFA Nations League", "season": 2026},
        "kickoff_utc": new_utc,
        "kickoff_madrid": new_mad,
        "selected": _ref(sel_dst),
        "source_handoff": _ref(handoff_dst),
    }
    (ev_dir / "case.json").write_text(json.dumps(case_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Simula inicialmente el estado corrupto heredado del bug (LINEUPS_PENDING + BLOCKED_MISSING_CONFIRMED_22_STARTERS)
    manifest_doc = {
        "event_id": "7905487",
        "url": "https://www.bwin.es/es/sports/eventos/gales-portugal-2:7905487",
        "status": "PREPARADO",
        "identity_status": "FIXTURE_AND_BWIN_TIME_VERIFIED",
        "kickoff_madrid": new_mad,
        "inputs": {
            "odds": _ref(odds_dst),
            "context": _ref(txt_dst),
            "context_json": _ref(json_dst),
            "identity": _ref(id_dst),
        },
        "steps": {"scraper": 0, "contexto": 0},
        "lineup_state": {
            "status": "LINEUPS_PENDING",
            "confirmed_complete_22": False,
            "validation_errors": ["API_LINEUP_UNEXPECTED_TEAM:id=767:name=Wales"],
        },
        "eligible_for_actionable_pick": False,
        "final_eligibility": "BLOCKED_MISSING_CONFIRMED_22_STARTERS",
    }
    (ev_dir / "manifest.json").write_text(json.dumps(manifest_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ev_dir": ev_dir, "txt": txt_dst, "json": json_dst, "identity": id_dst, "odds": odds_dst, "report": rep_dst}


class TestEvent7905487P0IntegrityAndSync(unittest.TestCase):
    def test_p0_a_team_identity_extraction_and_real_7905487_lineups(self):
        ctx_json_path = EVENT_7905487 / "context_updates" / "05239425bf28e513b86bec42" / "match_engine_7905487.json"
        if not ctx_json_path.is_file():
            self.skipTest("Snapshot real 7905487 no presente")

        ctx_doc = json.loads(ctx_json_path.read_text(encoding="utf-8"))
        ident_doc = json.loads((EVENT_7905487 / "context_updates" / "05239425bf28e513b86bec42" / "match_identity.json").read_text(encoding="utf-8"))
        team_ident = extract_fixture_team_identity(ctx_doc, identity_doc=ident_doc)
        self.assertEqual(team_ident["fixture_id"], 1528992)
        self.assertEqual(team_ident["home_id"], 767)
        self.assertEqual(team_ident["away_id"], 27)
        self.assertEqual(team_ident["home_name"], "Wales")
        self.assertEqual(team_ident["away_name"], "Portugal")
        self.assertIn("Gales", team_ident["home_aliases"])
        self.assertTrue(team_ident["is_national_team"])

        lu_eval = evaluate_fixture_lineup_state(
            api_lineups_response=ctx_doc["context"]["lineups"]["response"],
            fixture_id=team_ident["fixture_id"],
            event_id="7905487",
            home_id=team_ident["home_id"],
            away_id=team_ident["away_id"],
            home_name=team_ident["home_name"],
            away_name=team_ident["away_name"],
            home_aliases=team_ident["home_aliases"],
            away_aliases=team_ident["away_aliases"],
            is_national_team=team_ident["is_national_team"],
            kickoff_utc=team_ident["kickoff_utc"],
            now_utc="2026-11-17T19:21:40+00:00",
            api_acquired_at_utc="2026-11-17T19:16:29.265983+00:00",
        )
        self.assertEqual(lu_eval["status"], LineupStatus.LINEUPS_API_CONFIRMED.value)
        self.assertTrue(lu_eval["confirmed_complete_22"])
        self.assertEqual(lu_eval["starter_counts"], {"home": 11, "away": 11})
        self.assertEqual(lu_eval["validation_errors"], [])
        self.assertEqual(lu_eval["official_source_tier"], "STRUCTURED_PROVIDER")
        self.assertFalse(lu_eval["primary_official_verified"])
        self.assertEqual(lu_eval["real_source_verification"], "API_FOOTBALL_PROVIDER_RECORD_NOT_PRIMARY_CERTIFIED")
        self.assertNotIn("NOT_CLUB_CERTIFIED", lu_eval["real_source_verification"])

    def test_p0_b_safe_team_normalization_positive_and_negative_cases(self):
        side_wales = {"api_name": "Wales", "bwin_name": "Gales", "category": "male"}
        side_portugal = {"api_name": "Portugal", "bwin_name": "Portugal UEFA Nations League Fútbol", "category": "male"}

        # Positivos reales del evento 7905487 + variantes acentuadas/descriptivas
        self.assertEqual(mi.compare_report_name("Wales (Gales)", side_wales, side_portugal), "ALIAS")
        self.assertEqual(mi.compare_report_name("Portugal; selecciones absolutas masculinas", side_portugal, side_wales), "ALIAS")
        self.assertTrue(vp.same_club_report("Wales (Gales)", "Wales", "Gales"))
        self.assertTrue(vp.same_club_report("Portugal; selecciones absolutas masculinas", "Portugal", side_portugal["bwin_name"]))

        side_esp = {"api_name": "Spain", "bwin_name": "España", "category": "male"}
        side_bel = {"api_name": "Belgium", "bwin_name": "Bélgica", "category": "male"}
        self.assertEqual(mi.compare_report_name("España", side_esp, side_bel), "EXACT")
        self.assertEqual(mi.compare_report_name("Spain (España)", side_esp, side_bel), "ALIAS")
        self.assertEqual(mi.compare_report_name("Bélgica; selección absoluta masculina", side_bel, side_esp), "ALIAS")

        # Negativos estrictos: rechazar equipos distintos, categorías distintas y paréntesis cruzados
        for bad_name, expected_side, opp_side in (
            ("Spain; selecciones absolutas masculinas", side_portugal, side_wales),
            ("Portugal; selección femenina", side_portugal, side_wales),
            ("Portugal Sub-21", side_portugal, side_wales),
            ("Wales U19", side_wales, side_portugal),
            ("Wales (Portugal)", side_wales, side_portugal),
            ("Northern Ireland", {"api_name": "Ireland", "bwin_name": "Irlanda", "category": "male"}, side_wales),
            ("Real Sociedad", {"api_name": "Real Madrid", "bwin_name": "Real Madrid", "category": "male"}, side_bel),
        ):
            self.assertEqual(mi.compare_report_name(bad_name, expected_side, opp_side), "MISMATCH", bad_name)
            self.assertFalse(vp.same_club_report(bad_name, expected_side["api_name"], expected_side.get("bwin_name")), bad_name)

    def test_p0_c_three_provenance_levels_and_primary_requirement_rule(self):
        ctx_json_path = EVENT_7905487 / "context_updates" / "05239425bf28e513b86bec42" / "match_engine_7905487.json"
        if not ctx_json_path.is_file():
            self.skipTest("Snapshot real 7905487 no presente")
        ctx_doc = json.loads(ctx_json_path.read_text(encoding="utf-8"))
        team_ident = extract_fixture_team_identity(ctx_doc)

        # Cuando se exige fuente primaria explícitamente, API-Football conserva XI completos (no ausentes),
        # habilita análisis pero bloquea pick accionable con motivo explícito.
        res_strict = evaluate_fixture_lineup_state(
            api_lineups_response=ctx_doc["context"]["lineups"]["response"],
            fixture_id=1528992,
            event_id="7905487",
            home_id=767,
            away_id=27,
            home_name="Wales",
            away_name="Portugal",
            is_national_team=True,
            kickoff_utc="2026-11-17T19:45:00+00:00",
            now_utc="2026-11-17T19:20:00+00:00",
            require_primary_official=True,
        )
        self.assertTrue(res_strict["confirmed_complete_22"])
        self.assertEqual(res_strict["lineup_structural_status"], "COMPLETE_22_STRUCTURED")
        self.assertEqual(res_strict["official_source_tier"], "STRUCTURED_PROVIDER")
        self.assertFalse(res_strict["primary_official_verified"])
        self.assertTrue(res_strict["eligible_for_final_analysis"])
        self.assertFalse(res_strict["eligible_for_actionable_pick"])
        self.assertEqual(res_strict["reason"], "PRIMARY_OFFICIAL_SOURCE_REQUIRED_NOT_ARCHIVED")

        # Si hay adquisición posterior fallida, conserva la última adquisición verificable y su antigüedad
        res_fallback_stale = evaluate_fixture_lineup_state(
            api_lineups_response=[],
            fixture_id=1528992,
            event_id="7905487",
            home_id=767,
            away_id=27,
            home_name="Wales",
            away_name="Portugal",
            is_national_team=True,
            kickoff_utc="2026-11-17T19:45:00+00:00",
            now_utc="2026-11-17T19:33:00+00:00",  # T-12m (pasado T-15)
            last_verified_state={
                "status": "LINEUPS_API_CONFIRMED",
                "confirmed_complete_22": True,
                "source_origin": "API_FOOTBALL",
                "acquired_at_utc": "2026-11-17T18:20:00+00:00",  # 73m de antigüedad
            },
        )
        self.assertFalse(res_fallback_stale["confirmed_complete_22"])
        self.assertEqual(res_fallback_stale["last_verified_lineup"]["acquired_at_utc"], "2026-11-17T18:20:00+00:00")
        self.assertTrue(res_fallback_stale["last_verified_lineup"]["stale"])

    def test_p0_d_e_f_validator_differential_txt_report_0e5a_and_utc_fields(self):
        if not (EVENT_7905487 / "inputs" / "cuotas_bwin_7905487.txt").is_file():
            self.skipTest("Snapshot real 7905487 no presente")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Valida el informe inicial `0e5aeabe...` que contenía `Wales (Gales)` y `Portugal; selecciones absolutas masculinas`
            cloned = _clone_isolated_7905487(root, report_variant="0e5a")
            outdir = cloned["ev_dir"] / "validation"
            cmd = [
                sys.executable,
                str(ROOT / "validate_prematch.py"),
                "--root",
                str(root),
                "--url",
                "https://www.bwin.es/es/sports/eventos/gales-portugal-2:7905487",
                "--api",
                str(cloned["txt"]),
                "--odds",
                str(cloned["odds"]),
                "--report",
                str(cloned["report"]),
                "--identity",
                str(cloned["identity"]),
                "--historical",
                "--out",
                str(outdir),
            ]
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
            # Contrato P0-F: BLOCK=0, REVIEW=2 (REPORT_NO_CUTOFF + REPORT_COMPLETION_UNKNOWN) -> exit code 1 (REVISION_REQUERIDA)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            val_doc = json.loads((outdir / "validacion_7905487.json").read_text(encoding="utf-8"))
            self.assertEqual(val_doc["status"], "REVISION_REQUERIDA")

            codes = [i["code"] for i in val_doc["issues"]]
            self.assertNotIn("API_XI_ABSENT", codes)
            self.assertIn("API_XI_PRESENT_IN_CONTEXT", codes)
            self.assertNotIn("REPORT_WRONG_TEAM_HOME", codes)
            self.assertNotIn("REPORT_WRONG_TEAM_AWAY", codes)
            self.assertNotIn("IDENTITY_REPORT_MISMATCH_HOME", codes)
            self.assertNotIn("IDENTITY_REPORT_MISMATCH_AWAY", codes)
            self.assertIn("REPORT_NO_CUTOFF", codes)
            self.assertIn("REPORT_COMPLETION_UNKNOWN", codes)

            # P0-A: xi_reconciliation distingue cabecera antigua vs bloque diferencial presente
            self.assertFalse(val_doc["checks"]["xi_reconciliation"]["local"]["api_xi_reported_missing"])
            self.assertFalse(val_doc["checks"]["xi_reconciliation"]["visitante"]["api_xi_reported_missing"])
            self.assertTrue(val_doc["checks"]["xi_reconciliation"]["local"]["api_xi_initial_header_missing"])

            # P0-D: todos los campos *_utc están verdaderamente en +00:00, y *_madrid en horario local
            self.assertTrue(val_doc["checks"]["api"]["kickoff_utc"].endswith("+00:00"))
            self.assertTrue(val_doc["checks"]["api"]["kickoff_madrid"].endswith("+01:00"))
            self.assertTrue(val_doc["checks"]["odds"]["captured_utc"].endswith("+00:00"))

    def test_p0_a_d_f_full_end_to_end_reconcile_and_finish_without_error_767(self):
        if not (EVENT_7905487 / "inputs" / "cuotas_bwin_7905487.txt").is_file():
            self.skipTest("Snapshot real 7905487 no presente")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Copiamos programas mínimos reales necesarios para `prematch_multi.finish_one` en root aislado
            for mod in ("validate_prematch.py", "motor_probabilistico.py", "match_identity.py", "prematch_paths.py",
                        "prematch_context_delta.py", "prematch_cache_record.py", "prematch_case.py", "prematch_single.py",
                        "prematch_release.py", "prematch_tactical_signal_engine.py", "prematch_tactical_report_renderer.py",
                        "prematch_market_mapping_v1.py", "prematch_predictive_engine.py", "prematch_phase1.py",
                        "prematch_phase2.py", "prematch_tactical_history.py", "prematch_audit.py", "generate_context.py"):
                if (ROOT / mod).is_file():
                    shutil.copy2(ROOT / mod, root / mod)
            shutil.copytree(ROOT / "config", root / "config")
            shutil.copytree(ROOT / "bet_engine", root / "bet_engine")

            # Paso 1: reconciliación directa de manifest heredado con error espurio 767 en copia exacta de 7905487
            cloned = _clone_isolated_7905487(root, report_variant="15bb")
            # P0-D: Evaluar T-13m antes del kickoff (después de T-15, antes del inicio) con XI confirmados
            rec = pc.reconcile_case_state(
                root,
                "7905487",
                now_utc="2026-11-17T19:32:00+00:00",  # 20:32 CET (entre T-15 y kickoff 20:45!)
                lineup_cutoff_minutes=15,
            )
            self.assertEqual(rec["lineup_status"], "LINEUPS_API_CONFIRMED")
            self.assertTrue(rec["lineups_confirmed_22"])
            self.assertTrue(rec["eligible_for_final_analysis"])
            # Manifest aún estaba PREPARADO (sin finish), por lo que limpia bloqueo falso de XI sin fabricar actionable
            self.assertNotEqual(rec["final_eligibility"], "BLOCKED_MISSING_CONFIRMED_22_STARTERS")

            # Paso 2: ejecutar `finish_one` histórico (como auditoría completa Validación -> Legacy Motor -> Shadow)
            with contextlib.redirect_stdout(io.StringIO()):
                ok = pm.finish_one(
                    root,
                    "7905487",
                    sys.executable,
                    override_report=str(cloned["report"]),
                    historical=True,
                    shadow_mode=True,
                    lineup_cutoff_minutes=15,
                )
            self.assertTrue(ok)
            manifest_after = json.loads((cloned["ev_dir"] / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest_after["validation_summary"]["status"], "REVISION_REQUERIDA")
            self.assertEqual(manifest_after["validation_summary"]["exit_code"], 1)
            self.assertEqual(manifest_after["validation_summary"]["blocks"], 0)
            self.assertTrue(manifest_after["validation_summary"]["motor_allowed"])
            self.assertEqual(manifest_after["lineup_state"]["status"], "LINEUPS_API_CONFIRMED")
            self.assertTrue(manifest_after["lineup_state"]["confirmed_complete_22"])
            self.assertEqual(manifest_after["lineup_state"]["starter_counts"], {"home": 11, "away": 11})
            self.assertEqual(manifest_after["lineup_state"]["validation_errors"], [])
            self.assertEqual(manifest_after["final_eligibility"], "HISTORICAL_AUDIT_NO_LIVE_BET")

            shadow_doc = json.loads((cloned["ev_dir"] / "shadow" / "shadow_summary_7905487.json").read_text(encoding="utf-8"))
            self.assertEqual(shadow_doc["lineup_gate"]["status"], "LINEUPS_API_CONFIRMED")
            ranking_doc = json.loads((cloned["ev_dir"] / "probabilistic" / "ranking_probabilistico_7905487.json").read_text(encoding="utf-8"))
            self.assertEqual(ranking_doc["xi_api_status"], "PRESENTE_AMBOS")

    def test_differential_update_clears_txt_initial_missing_header_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ev_dir = root / "outputs" / "matches" / "7000099"
            ev_dir.mkdir(parents=True)
            ko = datetime(2026, 11, 20, 19, 45, tzinfo=timezone.utc)
            now = ko - timedelta(minutes=25)
            old_lu = {"get": "fixtures/lineups", "parameters": {"fixture": "555"}, "response": []}
            new_lu = {
                "get": "fixtures/lineups",
                "parameters": {"fixture": "555"},
                "_acquired_at_utc": now.isoformat(),
                "response": [
                    {
                        "team": {"id": 767, "name": "Wales"},
                        "formation": "4-2-3-1",
                        "coach": {"id": 0, "name": "C. Bellamy"},
                        "startXI": [{"player": {"id": 100 + i, "name": f"W{i}", "number": i + 1, "pos": "M", "grid": "1:1"}} for i in range(11)],
                    },
                    {
                        "team": {"id": 27, "name": "Portugal"},
                        "formation": "4-2-3-1",
                        "coach": {"id": 123, "name": "J. Jesus"},
                        "startXI": [{"player": {"id": 200 + i, "name": f"P{i}", "number": i + 1, "pos": "M", "grid": "1:1"}} for i in range(11)],
                    },
                ],
            }
            url = "https://www.bwin.es/es/sports/eventos/gales-portugal-2:7000099"
            base_json = {
                "generated_at": (ko - timedelta(hours=3)).isoformat(),
                "match": {"event_id": 7000099, "url": url, "home_name_raw": "Gales", "away_name_raw": "Portugal"},
                "fixture": {"fixture_id": 555, "date": ko.isoformat(), "home_id": 767, "home_name": "Wales", "away_id": 27, "away_name": "Portugal", "league_id": 5, "season": 2026},
                "context": {"lineups": old_lu},
            }
            txt_content = (
                "Event ID: 7000099\nFixture ID: 555\nDate UTC: "
                + ko.isoformat()
                + "\nHome: Wales (767)\nAway: Portugal (27)\n"
                + "AUDITORÍA DEL XI DEL FIXTURE OBJETIVO\n"
                + "HOME: XI NO DISPONIBLE (no confundir con no confirmado en otras fuentes)\n"
                + "AWAY: XI NO DISPONIBLE (no confundir con no confirmado en otras fuentes)\n"
                + "LINEUPS DEL FIXTURE OBJETIVO\n====================================\n"
                + json.dumps(old_lu)
                + "\n"
            )
            p_json = ev_dir / "match_engine_7000099.json"
            p_txt = ev_dir / "match_engine_7000099.txt"
            p_json.write_text(json.dumps(base_json), encoding="utf-8")
            p_txt.write_text(txt_content, encoding="utf-8")
            ctx_obj = {"root": root, "directory": ev_dir, "event_id": "7000099", "fixture_id": 555, "url": url, "kickoff": ko}
            manifest = {
                "status": "FINALIZADO",
                "final_eligibility": "BLOCKED_MISSING_CONFIRMED_22_STARTERS",
                "inputs": {"context_json": _ref(p_json), "context": _ref(p_txt)},
                "steps": {"validacion": 1, "motor": 0},
            }
            snap = root / "acq.json"
            snap.write_text(json.dumps(acquisition_record(new_lu, now)), encoding="utf-8")
            digest = _sha256(snap)
            updated_manifest, _ = pcd.refresh_lineups(ctx_obj, manifest, snap, digest, now)

            self.assertNotEqual(updated_manifest["final_eligibility"], "BLOCKED_MISSING_CONFIRMED_22_STARTERS")
            new_txt = Path(updated_manifest["inputs"]["context"]["path"]).read_text(encoding="utf-8")
            self.assertNotIn("HOME: XI NO DISPONIBLE", new_txt)
            self.assertIn("HOME: XI RECONCILIADO", new_txt)
            # Snapshot original intacto
            self.assertIn("HOME: XI NO DISPONIBLE", p_txt.read_text(encoding="utf-8"))
            meta = vp.api_meta(new_txt)
            self.assertFalse(meta["xi_api_home_missing"])
            self.assertFalse(meta["xi_api_away_missing"])

    def test_p0_d_t10_prematch_with_confirmed_xi_is_not_retrospective(self):
        if not (EVENT_7905487 / "inputs" / "cuotas_bwin_7905487.txt").is_file():
            self.skipTest("Snapshot real 7905487 no presente")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for mod in ("validate_prematch.py", "motor_probabilistico.py", "match_identity.py", "prematch_paths.py",
                        "prematch_context_delta.py", "prematch_cache_record.py", "prematch_case.py", "prematch_single.py",
                        "prematch_release.py", "prematch_tactical_signal_engine.py", "prematch_tactical_report_renderer.py",
                        "prematch_market_mapping_v1.py", "prematch_predictive_engine.py", "prematch_phase1.py",
                        "prematch_phase2.py", "prematch_tactical_history.py", "prematch_audit.py", "generate_context.py"):
                if (ROOT / mod).is_file():
                    shutil.copy2(ROOT / mod, root / mod)
            shutil.copytree(ROOT / "config", root / "config")
            shutil.copytree(ROOT / "bet_engine", root / "bet_engine")

            now_real = datetime.now(timezone.utc)
            ko_future = now_real + timedelta(minutes=11)  # T-11m: pasado T-15, pero ANTES del kickoff
            cloned = _clone_isolated_7905487(root, shift_kickoff_to=ko_future.isoformat(), report_variant="15bb")

            # Actualizamos cabecera de captura de cuotas para simular captura fresca en ventana T-60..T-15
            from zoneinfo import ZoneInfo

            mad_tz = ZoneInfo("Europe/Madrid")
            odds_txt = cloned["odds"].read_text(encoding="utf-8")
            fresh_cap = (now_real - timedelta(minutes=1)).astimezone(mad_tz).isoformat()
            odds_txt = odds_txt.replace("Captura: 2026-11-17T20:21:32+01:00", f"Captura: {fresh_cap}")
            cloned["odds"].write_text(odds_txt, encoding="utf-8")
            m_doc = json.loads((cloned["ev_dir"] / "manifest.json").read_text(encoding="utf-8"))
            m_doc["inputs"]["odds"] = {**_ref(cloned["odds"]), "generated_from": str(cloned["odds"])}
            (cloned["ev_dir"] / "manifest.json").write_text(json.dumps(m_doc, indent=2) + "\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                ok = pm.finish_one(
                    root,
                    "7905487",
                    sys.executable,
                    override_report=str(cloned["report"]),
                    historical=False,
                    shadow_mode=True,
                    lineup_cutoff_minutes=15,
                )
            self.assertTrue(ok)
            st = pc.status(root, "7905487")
            self.assertEqual(st["status"], "FINALIZADO")
            self.assertEqual(st["lineup_status"], "LINEUPS_API_CONFIRMED")
            self.assertTrue(st["lineups_confirmed_22"])
            self.assertEqual(st["closure_stage"], "FINAL_PREMATCH_VALID")
            self.assertTrue(st["final_prematch_valid"])
            self.assertTrue(st["eligible_for_final_analysis"])
            self.assertTrue(st["eligible_for_actionable_pick"])

            sh_doc = json.loads((cloned["ev_dir"] / "shadow" / "shadow_summary_7905487.json").read_text(encoding="utf-8"))
            self.assertFalse(sh_doc["historical_retrospective"])
            self.assertTrue(sh_doc["eligible_for_final_action"])


if __name__ == "__main__":
    unittest.main()
