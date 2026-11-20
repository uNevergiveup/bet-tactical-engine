"""Pruebas integrales P0: gestión obligatoria de XI confirmados, recuperación tardía,
alternativa manual estructurada, bloqueo sin 22 titulares y continuación automática.

Cubre los 10 escenarios obligatorios en modo estrictamente offline (sin llamadas externas).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import prematch_case as pc
import prematch_single as ps
from bet_engine.context import (
    CONFIRMED_LINEUP_STATUSES,
    LineupStatus,
    MatchObservationBundle,
    ObservationStore,
    compare_probable_vs_confirmed_absences,
    evaluate_fixture_lineup_state,
    load_and_validate_manual_lineup_file,
    select_next_eligible_ranked_candidate,
)
from bet_engine.contracts import MatchScheduleCandidate, ProximityPlanState, ScheduleExecutionStatus
from bet_engine.markets import BwinMarketEvaluator
from bet_engine.modeling import CoherentMatchModeler
from bet_engine.orchestration import PrematchLifecycleController, ShadowPipelineRunner, run_daily_shadow_lifecycle
from bet_engine.orchestration.shadow_pipeline import parse_bwin_odds_txt

ROOT = Path(__file__).resolve().parent.parent


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_11_players(prefix: str, start_id: int) -> list[dict]:
    return [
        {
            "player": {
                "id": start_id + i,
                "name": f"{prefix} Player {i + 1}",
                "number": i + 1,
                "pos": "G" if i == 0 else ("D" if i < 5 else ("M" if i < 9 else "F")),
                "grid": None,
            }
        }
        for i in range(11)
    ]


def _make_api_lineups_pair(home_id: int, home_name: str, away_id: int, away_name: str) -> list[dict]:
    return [
        {"team": {"id": home_id, "name": home_name}, "formation": "4-3-3", "coach": {"id": 101, "name": "Home Coach"}, "startXI": _make_11_players("Home", 1000)},
        {"team": {"id": away_id, "name": away_name}, "formation": "4-4-2", "coach": {"id": 202, "name": "Away Coach"}, "startXI": _make_11_players("Away", 2000)},
    ]


def _make_manual_lineup_doc(
    *,
    fixture_id: int,
    event_id: str,
    home_id: int,
    home_name: str,
    away_id: int,
    away_name: str,
    captured_at_utc: str,
    source: str = "Club Oficial / Acta AUF",
    source_url: str | None = "https://example.org/official-xi",
    source_kind: str = "OFFICIAL_CLUB",
    confirmed: bool | None = True,
) -> dict:
    doc = {
        "schema_version": "1.0-manual-lineup",
        "fixture_id": fixture_id,
        "event_id": event_id,
        "source": source,
        "source_url": source_url,
        "source_kind": source_kind,
        "captured_at_utc": captured_at_utc,
        "teams": {
            "home": {
                "team_id": home_id,
                "team_name": home_name,
                "starters": [{"id": 3000 + i, "name": f"ManualHome {i + 1}", "number": i + 1} for i in range(11)],
            },
            "away": {
                "team_id": away_id,
                "team_name": away_name,
                "starters": [{"id": 4000 + i, "name": f"ManualAway {i + 1}", "number": i + 1} for i in range(11)],
            },
        },
    }
    if confirmed is not None:
        doc["confirmed"] = confirmed
    return doc


def _build_isolated_case_from_7912816(root: Path, *, copy_context: bool = True) -> dict:
    src_dir = ROOT / "outputs/matches/7912816"
    me_src = src_dir / "inputs/match_engine_7912816.json"
    me_txt_src = src_dir / "inputs/match_engine_7912816.txt"
    odds_src = src_dir / "inputs/cuotas_bwin_7912816.txt"
    rep_src = src_dir / "inputs/reporte_cualitativo_7912816.md"
    me_doc = json.loads(me_src.read_text(encoding="utf-8"))

    b = ObservationStore.from_match_engine_json(me_doc)
    eid = "7912816"
    fid = int(b.fixture_id)
    mdir = root / "outputs/matches" / eid
    inputs_dir = mdir / "inputs"
    sel_dir = mdir / "selection" / "mock"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    sel_dir.mkdir(parents=True, exist_ok=True)

    odds_dst = inputs_dir / f"cuotas_bwin_{eid}.txt"
    odds_dst.write_bytes(odds_src.read_bytes())
    rep_dst = inputs_dir / f"reporte_cualitativo_{eid}.md"
    rep_dst.write_bytes(rep_src.read_bytes())

    bwin_url = (
        me_doc["match"].get("bwin_url")
        or me_doc["match"].get("url")
        or "https://www.bwin.es/es/sports/eventos/montevideo-wanderers-fc-juventud-de-las-piedras-2:7912816"
    )
    sel_doc = {
        "fixture_id": fid,
        "bwin_event_id": eid,
        "bwin_url": bwin_url,
        "home": b.home_name,
        "away": b.away_name,
        "home_api_id": b.home_id,
        "away_api_id": b.away_id,
        "league_id": b.league_id,
        "season": b.season,
        "kickoff_madrid": "2026-11-17T00:00:00+01:00",
    }
    sel_path = sel_dir / "selected_match.json"
    hoff_path = sel_dir / "handoff.json"
    sel_path.write_text(json.dumps(sel_doc, indent=2), encoding="utf-8")
    hoff_path.write_text(json.dumps(sel_doc, indent=2), encoding="utf-8")

    case_doc = {
        "schema_version": pc.SCHEMA,
        "event_id": eid,
        "fixture_id": fid,
        "bwin_url": bwin_url,
        "home": {"name": sel_doc["home"], "api_team_id": sel_doc["home_api_id"]},
        "away": {"name": sel_doc["away"], "api_team_id": sel_doc["away_api_id"]},
        "kickoff_madrid": sel_doc["kickoff_madrid"],
        "selected": pc.reference(sel_path),
        "source_handoff": pc.reference(hoff_path),
    }
    (mdir / "case.json").write_text(json.dumps(case_doc, indent=2), encoding="utf-8")

    inputs_manifest = {
        "odds": pc.reference(odds_dst),
        "report": pc.reference(rep_dst),
        "qualitative_research": pc.reference(rep_dst),
    }
    original_shas = {"odds": _sha256_bytes(odds_dst), "report": _sha256_bytes(rep_dst)}

    if copy_context:
        me_dst = inputs_dir / f"match_engine_{eid}.json"
        me_txt_dst = inputs_dir / f"match_engine_{eid}.txt"
        ident_dst = inputs_dir / "match_identity.json"
        me_dst.write_bytes(me_src.read_bytes())
        me_txt_dst.write_bytes(me_txt_src.read_bytes())
        from match_identity import identity_from_payload

        ident_dst.write_text(json.dumps(identity_from_payload(me_doc), indent=2), encoding="utf-8")
        inputs_manifest["context_json"] = pc.reference(me_dst)
        inputs_manifest["context"] = pc.reference(me_txt_dst)
        inputs_manifest["identity"] = pc.reference(ident_dst)
        original_shas["context_json"] = _sha256_bytes(me_dst)
        original_shas["context"] = _sha256_bytes(me_txt_dst)

    manifest_doc = {
        "schema_version": pc.SCHEMA,
        "event_id": eid,
        "fixture_id": fid,
        "url": bwin_url,
        "status": "PREPARADO",
        "kickoff_madrid": sel_doc["kickoff_madrid"],
        "inputs": inputs_manifest,
        "steps": {},
    }
    (mdir / "manifest.json").write_text(json.dumps(manifest_doc, indent=2), encoding="utf-8")
    return {
        "mdir": mdir,
        "me_doc": me_doc,
        "sel_doc": sel_doc,
        "odds_dst": odds_dst,
        "rep_dst": rep_dst,
        "original_shas": original_shas,
        "sel_path": sel_path,
    }


class LineupManagementP0Tests(unittest.TestCase):
    def test_01_reproduce_7912816_without_official_xi_blocks_actionable_pick(self) -> None:
        me_path = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        txt_path = ROOT / "outputs/matches/7912816/inputs/cuotas_bwin_7912816.txt"
        rep_path = ROOT / "outputs/matches/7912816/inputs/reporte_cualitativo_7912816.md"
        if not (me_path.is_file() and txt_path.is_file() and rep_path.is_file()):
            self.skipTest("Snapshot 7912816 no presente")

        # Antes del corte (107 min antes del kickoff) con solo 11+11 probables en reporte cualitativo -> LINEUPS_PROBABLE_ONLY
        me = json.loads(me_path.read_text(encoding="utf-8"))
        bundle_early = ObservationStore.from_match_engine_json(me, cutoff_utc="2026-11-16T21:13:04+00:00")
        mm = me.get("match") or {}
        odds = parse_bwin_odds_txt(
            txt_path,
            home_name=mm.get("home_name") or bundle_early.home_name,
            away_name=mm.get("away_name") or bundle_early.away_name,
        )
        qual_md = rep_path.read_text(encoding="utf-8")

        runner = ShadowPipelineRunner(min_lead_minutes=45, lineup_cutoff_minutes=15)
        rep_early = runner.run_shadow_on_bundle(
            bundle_early,
            target_status="NS",
            odds_map=odds,
            bwin_txt_path=txt_path,
            qualitative_markdown=qual_md,
        )
        self.assertEqual(rep_early.lineup_gate["status"], LineupStatus.LINEUPS_PROBABLE_ONLY.value)
        self.assertFalse(rep_early.lineup_gate["confirmed_complete_22"])
        self.assertFalse(rep_early.eligible_for_actionable_pick if hasattr(rep_early, "eligible_for_actionable_pick") else rep_early.eligible_for_final_action)
        self.assertFalse(rep_early.eligible_for_final_action)
        self.assertEqual(rep_early.decision_breakdown_counts["PICK_ACCIONABLE"], 0)
        self.assertEqual(rep_early.decision, "NO_BET")
        self.assertIn("LINEUPS_NOT_CONFIRMED:LINEUPS_PROBABLE_ONLY", rep_early.no_bet_reasons)

        # Dentro del corte final (<15 min antes del kickoff) sin XI confirmados -> LINEUPS_UNAVAILABLE_FINAL + BLOQUEO_TECNICO
        bundle_late = ObservationStore.from_match_engine_json(me, cutoff_utc="2026-11-16T22:48:00+00:00")
        rep_late = ShadowPipelineRunner(min_lead_minutes=10, lineup_cutoff_minutes=15).run_shadow_on_bundle(
            bundle_late,
            target_status="NS",
            odds_map=odds,
            bwin_txt_path=txt_path,
            qualitative_markdown=qual_md,
        )
        self.assertEqual(rep_late.lineup_gate["status"], LineupStatus.LINEUPS_UNAVAILABLE_FINAL.value)
        self.assertFalse(rep_late.eligible_for_final_action)
        self.assertEqual(rep_late.decision_taxonomy, "BLOQUEO_TECNICO")
        self.assertEqual(rep_late.decision_breakdown_counts["PICK_ACCIONABLE"], 0)

    def test_02_api_confirmed_11_plus_11_and_late_api_recovery_reevaluates_downstream(self) -> None:
        me_path = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        if not me_path.is_file():
            self.skipTest("Snapshot 7912816 no presente")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            setup = _build_isolated_case_from_7912816(root)
            mdir = setup["mdir"]
            me_doc = setup["me_doc"]
            sel = setup["sel_doc"]
            home_id = sel["home_api_id"]
            away_id = sel["away_api_id"]
            home_name = sel["home"]
            away_name = sel["away"]

            # Evaluador de mercados: sin XI bloquea PICK_ACCIONABLE aun con edge masivo; con 22 API sí lo autoriza
            bundle = ObservationStore.from_match_engine_json(me_doc, cutoff_utc="2026-11-16T22:15:00+00:00")
            model = CoherentMatchModeler.build_model(bundle)
            opts_no_xi = BwinMarketEvaluator.evaluate_standard_markets(
                bundle, model, odds_map={"home_win": 4.5, "draw": 6.0}, lineups_confirmed_complete=False, lineup_status="LINEUPS_PENDING"
            )
            self.assertTrue(all(o.decision_category != "PICK_ACCIONABLE" for o in opts_no_xi))

            # Simulamos recuperación tardía de API-Football mediante registro local datado antes del kickoff
            from prematch_cache_record import store_acquisition

            api_payload = {
                "get": "fixtures/lineups",
                "parameters": {"fixture": str(sel["fixture_id"])},
                "errors": [],
                "results": 2,
                "response": _make_api_lineups_pair(home_id, home_name, away_id, away_name),
            }
            snap_dir = mdir / "context_acquisitions"
            snap_file = store_acquisition(
                snap_dir, api_payload, now=datetime.fromisoformat("2026-11-16T22:10:00+00:00")
            )

            from prematch_context_delta import refresh_lineups

            manifest = json.loads((mdir / "manifest.json").read_text(encoding="utf-8"))
            ctx_obj = {
                "root": root,
                "directory": mdir,
                "event_id": "7912816",
                "fixture_id": int(sel["fixture_id"]),
                "url": setup["sel_doc"]["bwin_url"],
                "kickoff": datetime.fromisoformat("2026-11-16T23:00:00+00:00"),
            }
            updated_manifest, update_dir = refresh_lineups(
                ctx_obj, manifest, snap_file, _sha256_bytes(snap_file), datetime.fromisoformat("2026-11-16T22:12:00+00:00")
            )
            (mdir / "manifest.json").write_text(json.dumps(updated_manifest, indent=2), encoding="utf-8")
            self.assertEqual(updated_manifest["lineup_state"]["status"], LineupStatus.LINEUPS_API_CONFIRMED.value)
            self.assertTrue(updated_manifest["lineup_state"]["confirmed_complete_22"])

            # Snapshots originales en inputs/ permanecen 100% intactos (SHA idéntico)
            self.assertEqual(_sha256_bytes(mdir / "inputs/match_engine_7912816.json"), setup["original_shas"]["context_json"])
            self.assertTrue((update_dir / "match_engine_7912816.json").is_file())

            # Reevaluación downstream sobre el contexto actualizado reconoce LINEUPS_API_CONFIRMED
            updated_me = json.loads((update_dir / "match_engine_7912816.json").read_text(encoding="utf-8"))
            updated_bundle = ObservationStore.from_match_engine_json(updated_me, cutoff_utc="2026-11-16T22:15:00+00:00")
            self.assertEqual(updated_bundle.target_lineup_evaluation["status"], LineupStatus.LINEUPS_API_CONFIRMED.value)
            rep = ShadowPipelineRunner(min_lead_minutes=15).run_shadow_on_bundle(updated_bundle, odds_map={"over_2_5_goals": 1.9})
            self.assertEqual(rep.lineup_gate["status"], LineupStatus.LINEUPS_API_CONFIRMED.value)
            self.assertTrue(rep.eligible_for_final_action)

    def test_03_manual_lineup_11_plus_11_and_qualitative_confirmed_provenance(self) -> None:
        me_path = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        if not me_path.is_file():
            self.skipTest("Snapshot 7912816 no presente")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            setup = _build_isolated_case_from_7912816(root)
            mdir = setup["mdir"]
            me_doc = setup["me_doc"]
            sel = setup["sel_doc"]
            fid = int(sel["fixture_id"])
            hid = sel["home_api_id"]
            aid = sel["away_api_id"]
            hname = sel["home"]
            aname = sel["away"]

            manual_doc = _make_manual_lineup_doc(
                fixture_id=fid,
                event_id="7912816",
                home_id=hid,
                home_name=hname,
                away_id=aid,
                away_name=aname,
                captured_at_utc="2026-11-16T22:05:00+00:00",
                source="Comunicado oficial Montevideo Wanderers / AUF",
                source_kind="OFFICIAL_CLUB",
            )
            manual_path = root / "manual_xi.json"
            manual_path.write_text(json.dumps(manual_doc, indent=2), encoding="utf-8")

            res = pc.attach_manual_lineups(
                root,
                "7912816",
                manual_path,
                now_utc="2026-11-16T22:10:00+00:00",
                reevaluate=True,
            )
            self.assertEqual(res["lineup_state"]["status"], LineupStatus.LINEUPS_MANUAL_VERIFIED.value)
            self.assertTrue(res["lineup_state"]["confirmed_complete_22"])
            self.assertTrue(res["reevaluation"]["executed"])
            self.assertTrue(res["reevaluation"]["shadow"]["ok"])

            # Shadow recalculado automáticamente refleja LINEUPS_MANUAL_VERIFIED y elegibilidad final
            sh_summary = json.loads((mdir / "shadow/shadow_summary_7912816.json").read_text(encoding="utf-8"))
            self.assertEqual(sh_summary["lineup_gate"]["status"], LineupStatus.LINEUPS_MANUAL_VERIFIED.value)
            self.assertTrue(sh_summary["eligible_for_final_action"])

            # Inputs originales preservados sin sobrescritura
            self.assertEqual(_sha256_bytes(setup["odds_dst"]), setup["original_shas"]["odds"])
            self.assertEqual(_sha256_bytes(mdir / "inputs/match_engine_7912816.json"), setup["original_shas"]["context_json"])

        # Probar también EXTERNAL_PROVIDER -> LINEUPS_EXTERNAL_CONFIRMED + investigación cualitativa confirmada con procedencia
        ext_doc = _make_manual_lineup_doc(
            fixture_id=fid,
            event_id="7912816",
            home_id=hid,
            home_name=hname,
            away_id=aid,
            away_name=aname,
            captured_at_utc="2026-11-16T22:08:00+00:00",
            source="Sofascore Verified Feed",
            source_kind="EXTERNAL_PROVIDER",
        )
        with tempfile.TemporaryDirectory() as td2:
            ext_p = Path(td2) / "ext_xi.json"
            ext_p.write_text(json.dumps(ext_doc), encoding="utf-8")
            val_ext = load_and_validate_manual_lineup_file(
                ext_p,
                expected_fixture_id=fid,
                expected_event_id="7912816",
                expected_home_id=hid,
                expected_away_id=aid,
                kickoff_utc="2026-11-16T23:00:00+00:00",
            )
            self.assertEqual(val_ext["lineup_status"], LineupStatus.LINEUPS_EXTERNAL_CONFIRMED.value)

        # Investigación cualitativa con sección XI CONFIRMADO OFICIAL + fuente + captura prepartido
        rows_md = "\n".join(
            f"| {i+1} | Titular Local {i+1} | Titular Visitante {i+1} |" for i in range(11)
        )
        qual_confirmed_md = (
            f"**EVENT_ID_BWIN**: 7912816\n"
            f"**fixture_id API-Football**: {fid}\n\n"
            f"## XI CONFIRMADO OFICIAL\n"
            f"- Fuente: Tenfield / AUF Oficial\n"
            f"- URL: https://www.tenfield.com.uy/oficial\n"
            f"- Captura: 2026-11-16T22:15:00+00:00\n\n"
            f"| # | {hname} | {aname} |\n|---:|---|---|\n{rows_md}\n"
        )
        eval_q = evaluate_fixture_lineup_state(
            qualitative_markdown=qual_confirmed_md,
            fixture_id=fid,
            event_id="7912816",
            home_id=hid,
            away_id=aid,
            home_name=hname,
            away_name=aname,
            kickoff_utc="2026-11-16T23:00:00+00:00",
            now_utc="2026-11-16T22:20:00+00:00",
        )
        self.assertEqual(eval_q["status"], LineupStatus.LINEUPS_MANUAL_VERIFIED.value)
        self.assertTrue(eval_q["confirmed_complete_22"])

    def test_04_only_one_team_confirmed_stays_blocked(self) -> None:
        only_home = [{"team": {"id": 2360, "name": "Wanderers"}, "startXI": _make_11_players("OnlyHome", 5000)}]
        res_pending = evaluate_fixture_lineup_state(
            api_lineups_response=only_home,
            fixture_id=1649063,
            event_id="7912816",
            home_id=2360,
            away_id=2353,
            home_name="Wanderers",
            away_name="Juventud",
            kickoff_utc="2026-11-16T23:00:00+00:00",
            now_utc="2026-11-16T22:00:00+00:00",
            lineup_cutoff_minutes=15,
        )
        self.assertEqual(res_pending["status"], LineupStatus.LINEUPS_PENDING.value)
        self.assertFalse(res_pending["confirmed_complete_22"])
        self.assertTrue(res_pending["partial_team_confirmed"])
        self.assertEqual(res_pending["confirmed_sides"], {"home": True, "away": False})
        self.assertFalse(res_pending["eligible_for_actionable_pick"])

    def test_05_probable_xi_never_confirmed_and_absence_not_inferred_as_injury(self) -> None:
        # 1) Probable XI en archivo manual es rechazado cuando se exige confirmación
        prob_doc = _make_manual_lineup_doc(
            fixture_id=1649063,
            event_id="7912816",
            home_id=2360,
            home_name="Wanderers",
            away_id=2353,
            away_name="Juventud",
            captured_at_utc="2026-11-16T21:00:00+00:00",
            source="Diario Deportivo",
            source_kind="PROBABLE",
            confirmed=False,
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prob.json"
            p.write_text(json.dumps(prob_doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LINEUP_PROBABLE_CANNOT_BE_PROMOTED_TO_CONFIRMED"):
                load_and_validate_manual_lineup_file(
                    p,
                    expected_fixture_id=1649063,
                    expected_event_id="7912816",
                    kickoff_utc="2026-11-16T23:00:00+00:00",
                )

        # 2) Ausencia respecto al probable nunca se etiqueta como lesión o sanción
        omitted = compare_probable_vs_confirmed_absences(
            ["Player A", "Player Missing"],
            ["Player A", "Player Substitute"],
        )
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]["player_name"], "Player Missing")
        self.assertFalse(omitted[0]["inferred_injury"])
        self.assertFalse(omitted[0]["inferred_suspension"])
        self.assertEqual(omitted[0]["interpretation"], "NOT_INFERRED_AS_INJURY_OR_SUSPENSION")

    def test_06_07_08_adverse_post_kickoff_unverified_source_missing_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            setup = _build_isolated_case_from_7912816(root)
            sel = setup["sel_doc"]
            fid = int(sel["fixture_id"])
            hid, aid = sel["home_api_id"], sel["away_api_id"]
            hname, aname = sel["home"], sel["away"]

            # Caso 6: Captura posterior al kickoff
            late_doc = _make_manual_lineup_doc(
                fixture_id=fid, event_id="7912816", home_id=hid, home_name=hname, away_id=aid, away_name=aname,
                captured_at_utc="2026-11-16T23:05:00+00:00",
            )
            late_p = root / "late.json"
            late_p.write_text(json.dumps(late_doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LINEUP_CAPTURED_AFTER_KICKOFF"):
                pc.attach_manual_lineups(root, "7912816", late_p, now_utc="2026-11-16T23:10:00+00:00")

            # Caso 7a: Fuente sin identidad verificable
            nosrc_doc = _make_manual_lineup_doc(
                fixture_id=fid, event_id="7912816", home_id=hid, home_name=hname, away_id=aid, away_name=aname,
                captured_at_utc="2026-11-16T22:00:00+00:00", source="UNKNOWN",
            )
            nosrc_p = root / "nosrc.json"
            nosrc_p.write_text(json.dumps(nosrc_doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LINEUP_SOURCE_IDENTITY_MISSING"):
                pc.attach_manual_lineups(root, "7912816", nosrc_p, now_utc="2026-11-16T22:10:00+00:00")

            # Caso 7b: Fixture erróneo
            wrong_fid_doc = _make_manual_lineup_doc(
                fixture_id=9999999, event_id="7912816", home_id=hid, home_name=hname, away_id=aid, away_name=aname,
                captured_at_utc="2026-11-16T22:00:00+00:00",
            )
            wf_p = root / "wf.json"
            wf_p.write_text(json.dumps(wrong_fid_doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LINEUP_FIXTURE_MISMATCH"):
                pc.attach_manual_lineups(root, "7912816", wf_p, now_utc="2026-11-16T22:10:00+00:00")

            # Caso 8a: Faltan jugadores (<11)
            short_doc = _make_manual_lineup_doc(
                fixture_id=fid, event_id="7912816", home_id=hid, home_name=hname, away_id=aid, away_name=aname,
                captured_at_utc="2026-11-16T22:00:00+00:00",
            )
            short_doc["teams"]["home"]["starters"] = short_doc["teams"]["home"]["starters"][:10]
            sh_p = root / "short.json"
            sh_p.write_text(json.dumps(short_doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LINEUP_INCOMPLETE_TEAM"):
                pc.attach_manual_lineups(root, "7912816", sh_p, now_utc="2026-11-16T22:10:00+00:00")

            # Caso 8b: Jugador duplicado dentro del mismo equipo
            dup_doc = _make_manual_lineup_doc(
                fixture_id=fid, event_id="7912816", home_id=hid, home_name=hname, away_id=aid, away_name=aname,
                captured_at_utc="2026-11-16T22:00:00+00:00",
            )
            dup_doc["teams"]["away"]["starters"][1] = dict(dup_doc["teams"]["away"]["starters"][0])
            dup_p = root / "dup.json"
            dup_p.write_text(json.dumps(dup_doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LINEUP_DUPLICATE_PLAYER"):
                pc.attach_manual_lineups(root, "7912816", dup_p, now_utc="2026-11-16T22:10:00+00:00")

            # Caso 8c: Jugador duplicado entre local y visitante
            cross_doc = _make_manual_lineup_doc(
                fixture_id=fid, event_id="7912816", home_id=hid, home_name=hname, away_id=aid, away_name=aname,
                captured_at_utc="2026-11-16T22:00:00+00:00",
            )
            cross_doc["teams"]["away"]["starters"][0] = dict(cross_doc["teams"]["home"]["starters"][0])
            cross_p = root / "cross.json"
            cross_p.write_text(json.dumps(cross_doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LINEUP_CROSS_TEAM_DUPLICATE"):
                pc.attach_manual_lineups(root, "7912816", cross_p, now_utc="2026-11-16T22:10:00+00:00")

    def test_09_and_10_user_absent_and_automatic_reconsideration_of_next_candidate(self) -> None:
        me_path = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        if not me_path.is_file():
            self.skipTest("Snapshot 7912816 no presente")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            setup = _build_isolated_case_from_7912816(root)

            # Ranking con: #1 = 7912816 (descartado), #2 = sin margen suficiente (20m), #3 = elegible con 120m de margen
            ranking_path = root / "ranking.json"
            ranking_path.write_text(
                json.dumps(
                    {
                        "ranked_matches": [
                            {"fixture_id": 1649063, "bwin_event_id": "7912816", "kickoff_madrid": "2026-11-17T00:00:00+01:00", "eligible": True},
                            {"fixture_id": 888001, "bwin_event_id": "888001", "kickoff_madrid": "2026-11-16T23:10:00+00:00", "eligible": True},
                            {"fixture_id": 888002, "bwin_event_id": "888002", "kickoff_madrid": "2026-11-17T01:00:00+00:00", "eligible": True},
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            res = pc.resolve_lineups_or_fallback(
                root,
                "7912816",
                ranking_path=ranking_path,
                lineup_cutoff_minutes=15,
                min_lead_minutes=45,
                user_manual_absent=True,
                now_utc="2026-11-16T22:48:00+00:00",
            )
            self.assertEqual(res["lineup_status"], LineupStatus.LINEUPS_UNAVAILABLE_FINAL.value)
            self.assertFalse(res["confirmed_complete_22"])
            self.assertFalse(res["eligible_for_actionable_pick"])
            self.assertTrue(res["discarded"])
            # Salta #2 (888001 con 22m < 45m) y elige automáticamente #3 (888002 con 132m)
            nxt = res["next_candidate_reconsideration"]
            self.assertEqual(nxt["status"], "NEXT_CANDIDATE_SELECTED")
            self.assertEqual(nxt["selected_event_id"], "888002")
            self.assertEqual(nxt["selected_fixture_id"], 888002)

            # Snapshots originales de 7912816 preservados e intactos para auditoría
            self.assertEqual(_sha256_bytes(setup["odds_dst"]), setup["original_shas"]["odds"])
            self.assertEqual(_sha256_bytes(setup["rep_dst"]), setup["original_shas"]["report"])
            self.assertTrue(Path(res["audit_path"]).is_file())

            # También probar PrematchLifecycleController fallback automático cuando ev_1 falta XI en corte T-15m
            me = json.loads(me_path.read_text(encoding="utf-8"))
            b1 = replace(ObservationStore.from_match_engine_json(me, cutoff_utc="2026-11-16T22:48:00+00:00"), event_id="ev_1", kickoff_utc="2026-11-16T23:00:00+00:00")
            b2 = replace(b1, event_id="ev_2", fixture_id=999002, kickoff_utc="2026-11-17T01:30:00+00:00")
            ctrl = PrematchLifecycleController(Path(td) / "ws", available_from_local="13:00", min_quality_score=40.0, min_lead_minutes=10, lineup_cutoff_minutes=15)
            pre = ctrl.preanalyze(target_date_local="2026-11-16", bundles=[b1, b2], generated_at_utc="2026-11-16T10:00:00+00:00")
            pdict = pre["plan_state"]
            plan = ProximityPlanState(
                plan_id=pdict["plan_id"],
                target_date_local=pdict["target_date_local"],
                available_from_local=pdict["available_from_local"],
                timezone_name=pdict["timezone_name"],
                min_quality_score=pdict["min_quality_score"],
                candidates=tuple(
                    MatchScheduleCandidate(**{**c, "execution_status": ScheduleExecutionStatus(c["execution_status"])})
                    for c in pdict["candidates"]
                ),
            )
            rep1, plan_after, next_st = ctrl.finalize(
                plan_state=plan,
                bundle=b1,
                now_utc="2026-11-16T22:48:00+00:00",
                updated_odds_map={"over_2_5_goals": 1.9},
                fallback_on_missing_lineups=True,
                user_manual_absent=True,
            )
            self.assertFalse(rep1.eligible_for_final_action)
            self.assertIn("ev_1", plan_after.failed_event_ids)
            self.assertEqual(next_st["next_action"]["selected_event_id"], "ev_2")
            self.assertTrue((Path(td) / "ws" / "discarded_lineups_audit.json").is_file())


if __name__ == "__main__":
    unittest.main()
