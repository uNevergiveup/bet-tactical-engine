import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import prematch_case
import prematch_single
from bet_engine.context.lineup_manager import MANUAL_LINEUP_SCHEMA
from bet_engine.contracts.multi_slot import MultiMatchExecutionState, SlotCandidateRecord
from bet_engine.orchestration.multi_orchestrator import (
    MultiSlotOrchestrator,
    OrchestratorAdapters,
    OrchestratorConfig,
    _sync_canonical_manifest_fields,
    inspect_operational_case_artifacts,
)
from prematch_orchestrator import main as cli_main


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ref(path: Path) -> dict[str, str]:
    p = path.resolve()
    return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


def _players(prefix: str, start_id: int) -> list[dict]:
    positions = ["G", "D", "D", "D", "D", "M", "M", "M", "M", "F", "F"]
    return [
        {
            "player_id": start_id + idx,
            "name": f"{prefix} Player {idx}",
            "number": idx,
            "position": positions[idx - 1],
        }
        for idx in range(1, 12)
    ]


def _manual_xi_payload(
    *,
    event_id: int,
    fixture_id: int,
    home_id: int,
    away_id: int,
    home: str,
    away: str,
    kickoff_utc: str,
    captured_at_utc: str,
) -> dict:
    return {
        "schema_version": MANUAL_LINEUP_SCHEMA,
        "source": "MANUAL_OFFICIAL_LINEUP_VERIFIED",
        "source_kind": "MANUAL_VERIFIED",
        "confirmed": True,
        "event_id": str(event_id),
        "fixture_id": fixture_id,
        "kickoff_utc": kickoff_utc,
        "captured_at_utc": captured_at_utc,
        "teams": {
            "home": {"team_id": home_id, "team_name": home, "starters": _players(f"H{fixture_id}", 100), "substitutes": []},
            "away": {"team_id": away_id, "team_name": away, "starters": _players(f"A{fixture_id}", 200), "substitutes": []},
        },
    }


def _api_xi_response(home_id: int, away_id: int, home_name: str, away_name: str, fixture_id: int) -> list[dict]:
    return [
        {
            "team": {"id": home_id, "name": home_name},
            "startXI": [
                {"player": {"id": 1000 + i, "name": p["name"], "number": p["number"], "pos": p["position"]}}
                for i, p in enumerate(_players(f"H{fixture_id}", 100))
            ],
            "substitutes": [],
        },
        {
            "team": {"id": away_id, "name": away_name},
            "startXI": [
                {"player": {"id": 2000 + i, "name": p["name"], "number": p["number"], "pos": p["position"]}}
                for i, p in enumerate(_players(f"A{fixture_id}", 200))
            ],
            "substitutes": [],
        },
    ]


def _write_realistic_window_discovery(
    root: Path,
    *,
    day_label: str = "2026-11-17_1500_0300",
    subdir: str = "eval_001",
    matches: list[dict],
    bwin_rows: list[dict],
) -> tuple[Path, Path]:
    base = root / "outputs" / "discovery" / day_label / "evaluations" / subdir / "prospective_trial"
    pre_path = base / "enriched" / "preselection.json"
    bwin_path = base / "bwin" / "bwin_discovery.json"
    pre_path.parent.mkdir(parents=True, exist_ok=True)
    bwin_path.parent.mkdir(parents=True, exist_ok=True)
    pre_path.write_text(
        json.dumps({"generated_at_utc": "2026-11-17T14:00:00+00:00", "matches": matches}, indent=2),
        encoding="utf-8",
    )
    bwin_path.write_text(
        json.dumps({"generated_at_utc": "2026-11-17T14:00:00+00:00", "matches": bwin_rows}, indent=2),
        encoding="utf-8",
    )
    return pre_path, bwin_path


def _write_simulated_case_inputs(
    root: Path,
    *,
    event_id: int,
    fixture_id: int,
    bwin_url: str,
    home: str,
    away: str,
    home_id: int,
    away_id: int,
    kickoff_utc: str,
    captured_utc: str,
    mode: str = "PREPARADO",  # PREPARADO | PREPARADO_PROVISIONAL | PARCIAL_SIN_CUOTAS | ERROR_CONTEXTO
) -> dict:
    eid = str(event_id)
    ev_dir = root / "outputs" / "matches" / eid
    inputs_dir = ev_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    odds_ready = mode in {"PREPARADO", "PREPARADO_PROVISIONAL", "ERROR_CONTEXTO"}
    ctx_ready = mode in {"PREPARADO", "PREPARADO_PROVISIONAL", "PARCIAL_SIN_CUOTAS"}

    inputs_manifest: dict[str, dict] = {}
    if odds_ready:
        odds_path = inputs_dir / f"cuotas_bwin_{eid}.txt"
        odds_path.write_text(
            "\n".join(
                [
                    f"Event ID: {eid}",
                    f"URL: {bwin_url}",
                    f"Captura: {captured_utc}",
                    f"MATCH {home} vs {away}",
                    "Resultado del partido: 1 2.05 | X 3.30 | 2 3.60",
                    "Más/Menos goles: Over 2.5 1.98 | Under 2.5 1.82",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        inputs_manifest["odds"] = {**_ref(odds_path), "generated_from": str(odds_path)}

    identity_status = (
        "API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN"
        if mode == "PREPARADO_PROVISIONAL"
        else "FIXTURE_AND_BWIN_TIME_VERIFIED"
    )
    if ctx_ready:
        txt_path = inputs_dir / f"match_engine_{eid}.txt"
        json_path = inputs_dir / f"match_engine_{eid}.json"
        id_path = inputs_dir / "match_identity.json"
        txt_path.write_text(
            "\n".join(
                [
                    f"Event ID: {eid}",
                    f"Fixture ID: {fixture_id}",
                    f"Generado: {captured_utc}",
                    f"Match: {home} vs {away}",
                    "Cobertura: simulated_offline_snapshot",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        json_doc = {
            "generated_at": captured_utc,
            "match": {
                "event_id": eid,
                "url": bwin_url,
                "identity_status": identity_status,
                "home_name_raw": home,
                "away_name_raw": away,
            },
            "fixture": {
                "fixture_id": fixture_id,
                "date": kickoff_utc,
                "home_team": {"id": home_id, "name": home},
                "away_team": {"id": away_id, "name": away},
            },
        }
        json_path.write_text(json.dumps(json_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        id_doc = {
            "event_id": eid,
            "fixture_id": fixture_id,
            "url": bwin_url,
            "identity_status": identity_status,
        }
        id_path.write_text(json.dumps(id_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        inputs_manifest["context"] = {**_ref(txt_path), "generated_from": str(txt_path)}
        inputs_manifest["context_json"] = {**_ref(json_path), "generated_from": str(json_path)}
        inputs_manifest["identity"] = {**_ref(id_path), "generated_from": str(id_path)}

    manifest_path = ev_dir / "manifest.json"
    existing_manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    )
    new_manifest = {
        **existing_manifest,
        "event_id": eid,
        "fixture_id": fixture_id,
        "url": bwin_url,
        "prepared_at": captured_utc,
        "status": mode,
        "identity_status": identity_status if ctx_ready else existing_manifest.get("identity_status"),
        "inputs": inputs_manifest,
        "steps": {"scraper": 0 if odds_ready else 1, "contexto": 0 if ctx_ready else 1},
    }
    manifest_path.write_text(json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    case_path = ev_dir / "case.json"
    if case_path.is_file():
        case_doc = json.loads(case_path.read_text(encoding="utf-8"))
        _sync_canonical_manifest_fields(
            root,
            ev_dir,
            eid,
            {
                "case_path": str(case_path),
                "selected_match_path": (case_doc.get("selected") or {}).get("path"),
                "handoff_path": (case_doc.get("source_handoff") or {}).get("path"),
                "kickoff_madrid": case_doc.get("kickoff_madrid"),
            },
        )
    return new_manifest


class TestOperationalCaseIntegrationV3(unittest.TestCase):
    DAY = date(2026, 11, 17)
    LABEL = "2026-11-17_1500_0300"

    def _window_fixtures(self) -> tuple[list[dict], list[dict]]:
        matches = [
            {
                "fixture_id": 1647370,
                "bwin_event_id": 7909711,
                "home": "Saudi Arabia",
                "away": "Uzbekistan",
                "home_id": 23,
                "away_id": 1568,
                "league_id": 10,
                "league_name": "Friendlies",
                "season": 2026,
                "kickoff_utc": "2026-11-17T15:30:00+00:00",  # 16:30 CET -> FRANJA_A
                "data_readiness_score": 55.0,
                "research_priority_score": 70.0,
                "bwin_match_status": "IDENTITY_VERIFIED_AUTOMATICALLY",
            },
            {
                "fixture_id": 1528992,
                "bwin_event_id": 7905487,
                "home": "Wales",
                "away": "Portugal",
                "home_id": 767,
                "away_id": 27,
                "league_id": 10,
                "league_name": "Friendlies",
                "season": 2026,
                "kickoff_utc": "2026-11-17T19:45:00+00:00",  # 20:45 CET -> FRANJA_B
                "data_readiness_score": 45.0,
                "research_priority_score": 82.0,
                "bwin_match_status": "EVENT_FOUND_NEEDS_TIME_CONFIRMATION",
            },
            {
                "fixture_id": 1640519,
                "bwin_event_id": 7909714,
                "home": "Chile",
                "away": "Colombia",
                "home_id": 2383,
                "away_id": 8,
                "league_id": 10,
                "league_name": "Friendlies",
                "season": 2026,
                "kickoff_utc": "2026-11-17T23:00:00+00:00",  # 00:00 CET -> FRANJA_C
                "data_readiness_score": 45.0,
                "research_priority_score": 80.0,
                "bwin_match_status": "IDENTITY_VERIFIED_AUTOMATICALLY",
            },
            {
                "fixture_id": 1649999,
                "bwin_event_id": 7909999,
                "home": "Peru",
                "away": "Ecuador",
                "home_id": 30,
                "away_id": 2382,
                "league_id": 10,
                "league_name": "Friendlies",
                "season": 2026,
                "kickoff_utc": "2026-11-17T23:45:00+00:00",  # 00:45 CET -> FRANJA_C backup
                "data_readiness_score": 42.0,
                "research_priority_score": 68.0,
                "bwin_match_status": "IDENTITY_VERIFIED_AUTOMATICALLY",
            },
        ]
        bwin_rows = [
            {
                "fixture_id": 1647370,
                "bwin_event_id": "7909711",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/arabia-saudita-uzbekist%C3%A1n-2:7909711",
                "bwin_match_status": "IDENTITY_VERIFIED_AUTOMATICALLY",
            },
            {
                "fixture_id": 1528992,
                "bwin_event_id": "7905487",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/gales-portugal-2:7905487",
                "bwin_match_status": "EVENT_FOUND_NEEDS_TIME_CONFIRMATION",
            },
            {
                "fixture_id": 1640519,
                "bwin_event_id": "7909714",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/chile-colombia-2:7909714",
                "bwin_match_status": "IDENTITY_VERIFIED_AUTOMATICALLY",
            },
            {
                "fixture_id": 1649999,
                "bwin_event_id": "7909999",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/peru-ecuador-2:7909999",
                "bwin_match_status": "IDENTITY_VERIFIED_AUTOMATICALLY",
            },
        ]
        return matches, bwin_rows

    def test_offline_step_creates_canonical_case_and_never_fakes_waiting_research_without_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches, bwin_rows = self._window_fixtures()
            pre_path, _ = _write_realistic_window_discovery(root, matches=matches, bwin_rows=bwin_rows)

            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                rc = cli_main(
                    [
                        "step",
                        "--root",
                        str(root),
                        "--date",
                        "2026-11-17",
                        "--preselection",
                        str(pre_path),
                        "--require-research",
                        "--require-bwin",
                        "--offline",
                        "--now-utc",
                        "2026-11-17T13:30:00+00:00",  # 14:30 CET (> 60m antes de Arabia 16:30 CET)
                    ]
                )
            self.assertEqual(rc, 0)
            summary = json.loads(out_buf.getvalue())

            # Ninguno de los 3 puede pasar a WAITING_RESEARCH sin cuotas/contexto reales adquiridos
            for slot_row in summary["slots"]:
                eid = slot_row["active_or_closed_event_id"]
                self.assertEqual(slot_row["execution_state"], "PREPARING")
                self.assertEqual(slot_row["operational_stage"], "CASE_CREATED_PENDING_ACQUISITION")
                self.assertFalse(slot_row["data_ready"])
                self.assertFalse(slot_row["odds_ready"])
                self.assertFalse(slot_row["context_ready"])
                self.assertIsNotNone(slot_row["selected_match_path"])
                self.assertTrue(Path(slot_row["selected_match_path"]).is_file())

                ev_dir = root / "outputs" / "matches" / str(eid)
                case_doc = json.loads((ev_dir / "case.json").read_text(encoding="utf-8"))
                self.assertEqual(case_doc["schema_version"], "2.0-canonical-prematch-case")
                self.assertTrue((ev_dir / "research" / f"research_brief_{eid}.md").is_file())
                self.assertTrue((ev_dir / "lineups" / "manual" / f"manual_lineup_template_{eid}.json").is_file())

                # Validación directa por prematch_single y prematch_case
                ctx = prematch_single.load_context(
                    SimpleNamespace(root=root, selected=Path(slot_row["selected_match_path"]), decision=None)
                )
                self.assertEqual(ctx["event_id"], str(eid))
                if eid == 7905487:
                    self.assertEqual(ctx["selection_tier"], "B")
                else:
                    self.assertEqual(ctx["selection_tier"], "A")
                self.assertEqual(prematch_case.main(["--root", str(root), "status", "--event-id", str(eid)]), 0)

    def test_reconciles_incomplete_v3_stub_without_touching_historical_frozen_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches, bwin_rows = self._window_fixtures()
            pre_path, _ = _write_realistic_window_discovery(root, matches=matches, bwin_rows=bwin_rows)

            # Simula expediente histórico cerrado 7909704 que NO debe modificarse nunca
            hist_dir = root / "outputs" / "matches" / "7909704"
            (hist_dir / "inputs").mkdir(parents=True, exist_ok=True)
            hist_marker = hist_dir / "case.json"
            hist_bytes = b'{"schema_version": "2.0-canonical-prematch-case", "frozen_marker": true}\n'
            hist_marker.write_bytes(hist_bytes)

            # Simula bug previo en 7909711: case.json 3.0-slot-case sin selected_match ni inputs, pero plan en WAITING_RESEARCH
            stub_dir = root / "outputs" / "matches" / "7909711"
            (stub_dir / "inputs").mkdir(parents=True, exist_ok=True)
            (stub_dir / "case.json").write_text(
                json.dumps({"schema_version": "3.0-slot-case", "event_id": 7909711, "fixture_id": 1647370}),
                encoding="utf-8",
            )
            (stub_dir / "manifest.json").write_text(
                json.dumps({"schema_version": "1.0-event-manifest", "event_id": "7909711", "state": "PREPARED"}),
                encoding="utf-8",
            )

            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, require_research=True, require_bwin=True),
            )
            plan = orch.init_or_load_plan(now_utc=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc), explicit_preselection=pre_path)
            c_saudi = plan.get_candidate(1647370)
            assert c_saudi is not None
            c_saudi.execution_state = MultiMatchExecutionState.WAITING_RESEARCH
            c_saudi.research_state = "WAITING_RESEARCH"
            plan.active_by_slot["FRANJA_A"] = 1647370
            orch.save_plan(plan)

            reconciled = orch.step(now_utc=datetime(2026, 11, 17, 13, 5, tzinfo=timezone.utc))
            c_after = reconciled.get_candidate(1647370)
            self.assertEqual(c_after.execution_state, MultiMatchExecutionState.PREPARING)
            self.assertEqual(c_after.audit_outcome, "PENDING_DATA_ACQUISITION")
            archives = list((stub_dir / "archives").glob("v3_stub_*"))
            self.assertEqual(len(archives), 1)
            self.assertTrue((stub_dir / "selection" / "selected_match.json").is_file())
            self.assertEqual(hist_marker.read_bytes(), hist_bytes)

    def test_full_e2e_simulated_acquisition_research_xi_shadow_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches, bwin_rows = self._window_fixtures()
            # Prepara XI oficiales disponibles solo cuando corresponda vía fetcher controlado
            matches[0]["api_lineups_response"] = None  # Arabia usará recuperación manual
            matches[1]["api_lineups_response"] = _api_xi_response(767, 27, "Wales", "Portugal", 1528992)
            matches[2]["api_lineups_response"] = _api_xi_response(2383, 8, "Chile", "Colombia", 1640519)
            pre_path, _ = _write_realistic_window_discovery(root, matches=matches, bwin_rows=bwin_rows)

            acq_calls: list[int] = []
            shadow_calls: list[tuple[int, int]] = []

            def _sim_acquirer(r: Path, cand: SlotCandidateRecord, raw: dict, now: datetime) -> dict:
                acq_calls.append(cand.fixture_id)
                bwin_map = {
                    7909711: ("https://www.bwin.es/es/sports/eventos/arabia-saudita-uzbekist%C3%A1n-2:7909711", "PREPARADO"),
                    7905487: ("https://www.bwin.es/es/sports/eventos/gales-portugal-2:7905487", "PREPARADO_PROVISIONAL"),
                    7909714: ("https://www.bwin.es/es/sports/eventos/chile-colombia-2:7909714", "PREPARADO"),
                }
                url, st = bwin_map[int(cand.event_id)]
                manifest = _write_simulated_case_inputs(
                    r,
                    event_id=int(cand.event_id),
                    fixture_id=cand.fixture_id,
                    bwin_url=url,
                    home=cand.home_team,
                    away=cand.away_team,
                    home_id=int(raw["home_id"]),
                    away_id=int(raw["away_id"]),
                    kickoff_utc=cand.kickoff_utc,
                    captured_utc=now.isoformat(),
                    mode=st,
                )
                return {"action": "simulated_prepare", "status": manifest["status"]}

            def _sim_shadow(r: Path, cand: SlotCandidateRecord, now: datetime, cutoff: int, absent: bool) -> dict:
                shadow_calls.append((cand.fixture_id, int(cand.event_id or 0)))
                art = inspect_operational_case_artifacts(r, str(cand.event_id), require_full_artifacts=True)
                assert art["data_ready"]
                ev_analysis = r / "outputs" / "matches" / str(cand.event_id) / "analysis"
                ev_analysis.mkdir(parents=True, exist_ok=True)
                (ev_analysis / "shadow_isolation_marker.json").write_text(
                    json.dumps({"event_id": cand.event_id, "fixture_id": cand.fixture_id, "manifest": art["manifest_status"]}),
                    encoding="utf-8",
                )
                if cand.fixture_id == 1647370:
                    return {"status": "OK", "decision_taxonomy": "PICK_ACCIONABLE", "candidate": {"market": "1X2", "odds": 2.05}}
                if cand.fixture_id == 1528992:
                    # Incluso si Shadow intentara actionable, PREPARADO_PROVISIONAL (Tier B sin hora Bwin) lo degrada!
                    return {"status": "OK", "decision_taxonomy": "PICK_ACCIONABLE", "candidate": {"market": "1X2", "odds": 1.85}}
                return {"status": "OK", "decision_taxonomy": "NO_BET_POR_PRECIO", "candidate": None}

            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, require_research=True, require_bwin=True),
                adapters=OrchestratorAdapters(context_and_odds_acquirer=_sim_acquirer, shadow_evaluator=_sim_shadow),
            )
            # Fase 1 + 1.5 -> adquiere datos reales y pausa en WAITING_RESEARCH
            t0 = datetime(2026, 11, 17, 13, 30, tzinfo=timezone.utc)
            plan = orch.run_cycle(now_utc=t0, explicit_preselection=pre_path)
            self.assertEqual(sorted(acq_calls), [1528992, 1640519, 1647370])
            for fid in (1647370, 1528992, 1640519):
                self.assertEqual(plan.get_candidate(fid).execution_state, MultiMatchExecutionState.WAITING_RESEARCH)
                self.assertEqual(plan.get_candidate(fid).odds_state, "CAPTURED_VALID")

            # Rechaza informe cualitativo sin identidad correcta
            bad_res = root / "bad_research.md"
            bad_res.write_text("# Informe sin IDs\nTexto genérico.\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                orch.attach_research_file(event_id=7909711, research_path=bad_res, now_utc=t0)

            # Adjunta informes válidos con Event ID + Fixture ID verificables via prematch_case.attach_research
            for fid, eid, ko in (
                (1647370, 7909711, "2026-11-17T15:30:00+00:00"),
                (1528992, 7905487, "2026-11-17T19:45:00+00:00"),
                (1640519, 7909714, "2026-11-17T23:00:00+00:00"),
            ):
                res_file = root / f"research_{eid}.md"
                res_file.write_text(
                    "\n".join(
                        [
                            f"# Análisis Cualitativo {eid}",
                            f"- Event ID: {eid}",
                            f"- Fixture ID: {fid}",
                            f"- Kickoff UTC: {ko}",
                            "- Report Generated UTC: 2026-11-17T13:35:00+00:00",
                            "",
                            "Fuentes: reporte oficial federativo previo al encuentro.",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                orch.attach_research_file(
                    event_id=eid,
                    research_path=res_file,
                    now_utc=datetime(2026, 11, 17, 13, 36, tzinfo=timezone.utc),
                )

            # Paso 2: Wales y Chile tienen XI API -> finalizan (Wales degradado por provisional); Arabia espera XI manual!
            t1 = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)
            plan = orch.run_cycle(now_utc=t1)
            self.assertEqual(plan.get_candidate(1647370).execution_state, MultiMatchExecutionState.WAITING_LINEUPS)
            self.assertEqual(plan.get_candidate(1528992).execution_state, MultiMatchExecutionState.NO_BET)
            self.assertEqual(plan.get_candidate(1528992).shadow_decision, "CANDIDATO_CONDICIONAL")
            self.assertIn("BWIN_KICKOFF_UNVERIFIED_PROVISIONAL", plan.get_candidate(1528992).blocking_reasons)
            self.assertEqual(plan.get_candidate(1640519).execution_state, MultiMatchExecutionState.NO_BET)
            self.assertEqual(plan.get_candidate(1640519).shadow_decision, "NO_BET_POR_PRECIO")

            # Incorpora XI manual verificado para Arabia (7909711) -> FINALIZED con PICK_ACCIONABLE
            lu_file = root / "manual_7909711.json"
            lu_file.write_text(
                json.dumps(
                    _manual_xi_payload(
                        event_id=7909711,
                        fixture_id=1647370,
                        home_id=23,
                        away_id=1568,
                        home="Saudi Arabia",
                        away="Uzbekistan",
                        kickoff_utc="2026-11-17T15:30:00+00:00",
                        captured_at_utc="2026-11-17T14:25:00+00:00",
                    )
                ),
                encoding="utf-8",
            )
            plan = orch.attach_manual_lineup_file(
                event_id=7909711,
                manual_json_path=lu_file,
                now_utc=datetime(2026, 11, 17, 14, 26, tzinfo=timezone.utc),
            )
            self.assertEqual(plan.get_candidate(1647370).execution_state, MultiMatchExecutionState.FINALIZED)
            self.assertEqual(plan.get_candidate(1647370).shadow_decision, "PICK_ACCIONABLE")

            # Reanudación idempotente: no repite adquisiciones ni Shadow!
            prev_acq = len(acq_calls)
            prev_sh = len(shadow_calls)
            plan_resumed = orch.run_cycle(now_utc=datetime(2026, 11, 17, 14, 30, tzinfo=timezone.utc))
            self.assertEqual(len(acq_calls), prev_acq)
            self.assertEqual(len(shadow_calls), prev_sh)
            self.assertEqual(plan_resumed.get_candidate(1647370).execution_state, MultiMatchExecutionState.FINALIZED)

    def test_partial_provider_error_lead_guard_and_slot_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches, bwin_rows = self._window_fixtures()
            # 15:00 UTC = 16:00 CET -> Arabia (16:30 CET) solo tiene 30m (< 45m min_lead!) -> NO forzar adquisición!
            # Wales-Portugal (20:45 CET) sufre PARCIAL_SIN_CUOTAS en intento 1 y recupera en intento 2 sin bloquear FRANJA_C!
            # Chile-Colombia (00:00 CET) queda sin XI en T-10m -> fallback a Peru-Ecuador (00:45 CET) que sí tiene >=45m y XI API.
            matches[1]["api_lineups_response"] = _api_xi_response(767, 27, "Wales", "Portugal", 1528992)
            matches[2]["api_lineups_response"] = None
            matches[3]["api_lineups_response"] = _api_xi_response(30, 2382, "Peru", "Ecuador", 1649999)
            pre_path, _ = _write_realistic_window_discovery(root, matches=matches, bwin_rows=bwin_rows)

            wales_calls = 0

            def _partial_then_ok_acquirer(r: Path, cand: SlotCandidateRecord, raw: dict, now: datetime) -> dict:
                nonlocal wales_calls
                eid = int(cand.event_id)
                url = next(b["bwin_url"] for b in bwin_rows if b["fixture_id"] == cand.fixture_id)
                mode = "PREPARADO"
                if eid == 7905487:
                    wales_calls += 1
                    mode = "PARCIAL_SIN_CUOTAS" if wales_calls == 1 else "PREPARADO"
                _write_simulated_case_inputs(
                    r,
                    event_id=eid,
                    fixture_id=cand.fixture_id,
                    bwin_url=url,
                    home=cand.home_team,
                    away=cand.away_team,
                    home_id=int(raw["home_id"]),
                    away_id=int(raw["away_id"]),
                    kickoff_utc=cand.kickoff_utc,
                    captured_utc=now.isoformat(),
                    mode=mode,
                )
                return {"action": "simulated", "status": mode}

            def _shadow_ok(r: Path, cand: SlotCandidateRecord, now: datetime, cutoff: int, absent: bool) -> dict:
                return {"status": "OK", "decision_taxonomy": "NO_BET_POR_PRECIO", "candidate": None}

            orch = MultiSlotOrchestrator(
                root,
                OrchestratorConfig(window_day=self.DAY, min_lead_minutes=45, lineup_cutoff_minutes=15, require_bwin=True),
                adapters=OrchestratorAdapters(context_and_odds_acquirer=_partial_then_ok_acquirer, shadow_evaluator=_shadow_ok),
            )
            # Ejecución en 14:55 UTC (Arabia tiene 35m < 45m, queda inelegible sin tocar API/Bwin!)
            p1 = orch.step(now_utc=datetime(2026, 11, 17, 14, 55, tzinfo=timezone.utc), explicit_preselection=pre_path)
            self.assertEqual(p1.get_candidate(1647370).execution_state, MultiMatchExecutionState.SKIPPED_INELIGIBLE)
            self.assertFalse((root / "outputs" / "matches" / "7909711" / "inputs" / "cuotas_bwin_7909711.txt").exists())

            # Wales tuvo PARCIAL_SIN_CUOTAS: conserva contexto API y queda en PREPARING sin saltar a XI
            self.assertEqual(p1.get_candidate(1528992).execution_state, MultiMatchExecutionState.PREPARING)
            self.assertEqual(p1.get_candidate(1528992).audit_outcome, "ACQUISITION_INCOMPLETE:PARCIAL_SIN_CUOTAS")
            self.assertTrue((root / "outputs" / "matches" / "7905487" / "inputs" / "match_engine_7905487.json").is_file())
            # Chile adquirió completo y quedó en WAITING_LINEUPS
            self.assertEqual(p1.get_candidate(1640519).execution_state, MultiMatchExecutionState.WAITING_LINEUPS)

            # Reintento en 15:05 UTC: Wales recupera cuotas -> avanza -> NO_BET
            p2 = orch.step(now_utc=datetime(2026, 11, 17, 15, 5, tzinfo=timezone.utc))
            self.assertEqual(p2.get_candidate(1528992).execution_state, MultiMatchExecutionState.NO_BET)

            # T-10m de Chile (22:50 UTC) sin XI -> REJECTED_MISSING_LINEUPS -> fallback a Peru-Ecuador (T-55m >=45m!) -> adquiere & finaliza!
            p3 = orch.run_cycle(now_utc=datetime(2026, 11, 17, 22, 50, tzinfo=timezone.utc))
            self.assertEqual(p3.get_candidate(1640519).execution_state, MultiMatchExecutionState.REJECTED_MISSING_LINEUPS)
            self.assertEqual(p3.get_candidate(1649999).fallback_parent_fixture_id, 1640519)
            self.assertEqual(p3.get_candidate(1649999).execution_state, MultiMatchExecutionState.NO_BET)


if __name__ == "__main__":
    unittest.main()
