#!/usr/bin/env python3
"""Pruebas Iteración 2.2C:
1. Persistencia, inmutabilidad en archivo separado y hashes de integridad del registro prospectivo.
2. Verificación de resultados deportivos (VERIFIED_AUTHORITATIVE vs UNVERIFIED_RESULT vs SIMULATED_TEST_ONLY).
3. Auditoría temporal Elo (simultáneos, finalizaciones tardías, prórroga, interrupciones y publicación tardía).
4. Independencia respecto a NO BET de Shadow y prueba sintética CLI end-to-end.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.modeling.experimental_v2_1b import AsOfEloTracker, resolve_elo_available_dt
from bet_engine.modeling.prospective_registry import (
    CORPUS_CUTOFF_UTC,
    FROZEN_MODEL_NAMES,
    ProspectiveRegistryError,
    evaluate_prospective_records,
    record_prospective_prediction,
    settle_prospective_record,
)

ROOT = Path(__file__).resolve().parent.parent


def _make_auth_fixture(pre: dict, hg: int, ag: int, status: str = "FT") -> dict:
    return {
        "get": "fixtures",
        "parameters": {"id": str(pre["fixture_id"])},
        "_provenance": {"provider": "API-Football", "captured_at_utc": "2026-11-25T22:25:00+00:00"},
        "response": [
            {
                "fixture": {"id": pre["fixture_id"], "status": {"short": status}},
                "league": {"id": pre["competition"]["league_id"]},
                "teams": {
                    "home": {"id": pre["teams"]["home_id"]},
                    "away": {"id": pre["teams"]["away_id"]},
                },
                "goals": {"home": hg, "away": ag},
            }
        ],
    }


class TestIteracion22CRegistryAndElo(unittest.TestCase):
    def _load_sample_payload(self) -> tuple[dict, Path, datetime]:
        me_path = ROOT / "outputs/matches/7905487/inputs/match_engine_7905487.json"
        manifest_path = ROOT / "audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json"
        if not (me_path.is_file() and manifest_path.is_file()):
            self.skipTest("Fixtures o manifest no presentes")
        base_payload = json.loads(me_path.read_text(encoding="utf-8"))
        future_ko = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
        payload = copy.deepcopy(base_payload)
        payload["fixture"]["date"] = future_ko.isoformat()
        payload["context"]["target_fixture_full"] = {
            "response": [
                {
                    "fixture": {"id": payload["fixture"]["fixture_id"], "date": future_ko.isoformat(), "status": {"short": "NS"}},
                    "league": {"id": payload["fixture"]["league_id"], "season": payload["fixture"]["season"]},
                    "teams": {
                        "home": {"id": payload["fixture"]["home_id"]},
                        "away": {"id": payload["fixture"]["away_id"]},
                    },
                    "goals": {"home": None, "away": None},
                }
            ]
        }
        return payload, manifest_path, future_ko

    def test_persistence_hashes_equal_info_and_separate_settlement_immutability(self) -> None:
        payload, manifest_path, future_ko = self._load_sample_payload()
        now_pre = future_ko - timedelta(hours=2)
        payload["generated_at"] = (now_pre - timedelta(minutes=10)).isoformat()

        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "records"

            # 1. El corte del corpus NO sustituye now_utc < kickoff_utc:
            #    kickoff > CORPUS_CUTOFF_UTC pero now_utc >= kickoff debe rechazarse.
            self.assertGreater(future_ko.isoformat(), CORPUS_CUTOFF_UTC)
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(
                    match_engine_payload=payload,
                    manifest_path=manifest_path,
                    records_dir=rec_dir,
                    now_utc=future_ko + timedelta(seconds=1),
                )

            # 2. Registro válido antes del kickoff
            rec_path = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=rec_dir,
                corpus_root=ROOT,
                bwin_odds={"home_win": 2.40, "draw": 3.10, "away_win": 3.00},
                odds_captured_utc=now_pre.isoformat(),
                odds_sha256="abc123",
                shadow_decision="NO_BET",
                now_utc=now_pre,
            )
            raw_before = rec_path.read_bytes()
            sha_before = hashlib.sha256(raw_before).hexdigest()
            doc = json.loads(raw_before.decode("utf-8"))
            pre = doc["pre_kickoff"]

            # 3. Hashes cubren datos, versiones, parámetros y procedencia
            prov = pre["provenance"]
            for req_key in (
                "registry_schema_version",
                "input_sha256",
                "match_engine_payload_sha256",
                "observations_sha256",
                "odds_sha256",
                "manifest_sha256",
                "model_params_sha256",
                "frozen_manifest_version",
                "elo_builder_meta",
            ):
                self.assertIn(req_key, prov)
                self.assertTrue(prov[req_key])

            # 4. Todos los modelos reciben exactamente el mismo estado informativo
            info_hashes = {pre["predictions"][m]["info_state_sha256"] for m in FROZEN_MODEL_NAMES}
            self.assertEqual(len(info_hashes), 1)
            self.assertEqual(next(iter(info_hashes)), prov["observations_sha256"])

            # 5. No permite sobrescribir ni siquiera con allow_overwrite=True
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(
                    match_engine_payload=payload,
                    manifest_path=manifest_path,
                    records_dir=rec_dir,
                    now_utc=now_pre,
                )
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(
                    match_engine_payload=payload,
                    manifest_path=manifest_path,
                    records_dir=rec_dir,
                    now_utc=now_pre,
                    allow_overwrite=True,
                )

            # 6. settle guarda en archivo separado SIN modificar rec_path ni un solo byte
            auth_fx = _make_auth_fixture(pre, 2, 1, "FT")
            settled = settle_prospective_record(
                rec_path,
                hg=2,
                ag=1,
                match_status="FT",
                settled_at_utc=future_ko + timedelta(minutes=150),
                authoritative_fixture=auth_fx,
            )
            raw_after = rec_path.read_bytes()
            sha_after = hashlib.sha256(raw_after).hexdigest()
            self.assertEqual(sha_before, sha_after)
            self.assertEqual(raw_before, raw_after)
            self.assertTrue(Path(settled["settlement_path"]).is_file())
            self.assertNotEqual(Path(settled["settlement_path"]).resolve(), rec_path.resolve())

            # 7. Evaluación incluye el partido aunque Shadow fuera NO_BET
            ev = evaluate_prospective_records(rec_dir)
            self.assertEqual(ev["settled_paired_n"], 1)
            self.assertTrue(ev["evaluation_independent_of_shadow_decision"])

            # 8. Cualquier modificación posterior de rec_path invalida la integridad
            tampered = json.loads(raw_after.decode("utf-8"))
            tampered["pre_kickoff"]["predictions"]["M0"]["p_over_2_5"] = 0.01
            rec_path.write_text(json.dumps(tampered), encoding="utf-8")
            ev_tampered = evaluate_prospective_records(rec_dir)
            self.assertEqual(len(ev_tampered["tampered_rejected"]), 1)
            self.assertEqual(ev_tampered["settled_paired_n"], 0)

    def test_result_verification_unverified_and_simulation_separation(self) -> None:
        payload, manifest_path, future_ko = self._load_sample_payload()
        now_pre = future_ko - timedelta(hours=2)
        payload["generated_at"] = (now_pre - timedelta(minutes=5)).isoformat()

        with tempfile.TemporaryDirectory() as td:
            # Caso A: entrada manual sin fuente autorizada -> UNVERIFIED_RESULT (no acreditable)
            dir_unv = Path(td) / "unverified"
            rec_u = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=dir_unv,
                now_utc=now_pre,
            )
            res_u = settle_prospective_record(
                rec_u,
                hg=1,
                ag=1,
                match_status="FT",
                settled_at_utc=future_ko + timedelta(minutes=140),
            )
            self.assertEqual(res_u["settlement"]["verification_status"], "UNVERIFIED_RESULT")
            self.assertFalse(res_u["settlement"]["creditable"])
            ev_u = evaluate_prospective_records(dir_unv)
            self.assertEqual(ev_u["settled_paired_n"], 0)
            self.assertEqual(ev_u["unverified_result_n"], 1)

            # Caso B: modo simulación -> SIMULATED_TEST_ONLY (no acreditable)
            dir_sim = Path(td) / "simulated"
            rec_s = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=dir_sim,
                now_utc=now_pre,
            )
            res_s = settle_prospective_record(
                rec_s,
                hg=3,
                ag=0,
                match_status="FT",
                settled_at_utc=future_ko + timedelta(minutes=140),
                simulation_mode=True,
            )
            self.assertEqual(res_s["settlement"]["verification_status"], "SIMULATED_TEST_ONLY")
            self.assertTrue(res_s["settlement"]["is_simulation"])
            self.assertFalse(res_s["settlement"]["creditable"])
            ev_s = evaluate_prospective_records(dir_sim)
            self.assertEqual(ev_s["settled_paired_n"], 0)
            self.assertEqual(ev_s["simulated_test_n"], 1)

            # Caso C: discrepancia con fuente autorizada -> rechaza liquidación
            dir_mis = Path(td) / "mismatch"
            rec_m = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=dir_mis,
                now_utc=now_pre,
            )
            pre_m = json.loads(rec_m.read_text(encoding="utf-8"))["pre_kickoff"]
            bad_auth = _make_auth_fixture(pre_m, 0, 2, "FT")  # marcador distinto a 2-1
            with self.assertRaises(ProspectiveRegistryError):
                settle_prospective_record(
                    rec_m,
                    hg=2,
                    ag=1,
                    match_status="FT",
                    settled_at_utc=future_ko + timedelta(minutes=140),
                    authoritative_fixture=bad_auth,
                )

    def test_elo_temporal_simultaneous_late_extra_time_interruptions_and_delayed_pub(self) -> None:
        t0 = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)
        fixtures = [
            # Dos partidos simultáneos al mismo kickoff
            {"fixture_id": 101, "home_id": 1, "away_id": 2, "hg": 3, "ag": 0, "stratum": "CLUB", "ko_dt": t0, "status": "FT"},
            {"fixture_id": 102, "home_id": 3, "away_id": 4, "hg": 0, "ag": 2, "stratum": "CLUB", "ko_dt": t0, "status": "FT"},
            # Finalización tardía publicada después del offset estimado +125m
            {
                "fixture_id": 103,
                "home_id": 5,
                "away_id": 6,
                "hg": 2,
                "ag": 0,
                "stratum": "CLUB",
                "ko_dt": t0,
                "status": "FT",
                "published_at_utc": (t0 + timedelta(minutes=200)).isoformat(),
            },
            # Prórroga AET
            {"fixture_id": 104, "home_id": 7, "away_id": 8, "hg": 2, "ag": 1, "stratum": "CLUB", "ko_dt": t0, "status": "AET"},
            # Interrumpido / Suspendido / Aplazado -> excluidos
            {"fixture_id": 105, "home_id": 9, "away_id": 10, "hg": 1, "ag": 0, "stratum": "CLUB", "ko_dt": t0, "status": "INT"},
            {"fixture_id": 106, "home_id": 11, "away_id": 12, "hg": 0, "ag": 0, "stratum": "CLUB", "ko_dt": t0, "status": "SUSP"},
        ]
        tracker = AsOfEloTracker.build_from_fixtures(fixtures)
        self.assertEqual(tracker.excluded_non_finished, 2)

        # Simultaneous: antes de t0+125m ninguno tiene actualización
        self.assertEqual(tracker.get_rating_asof("CLUB", 1, t0 + timedelta(minutes=124))[1], 0)
        self.assertEqual(tracker.get_rating_asof("CLUB", 3, t0 + timedelta(minutes=124))[1], 0)
        # A t0+125m entran ambos simultáneos
        self.assertEqual(tracker.get_rating_asof("CLUB", 1, t0 + timedelta(minutes=125))[1], 1)
        self.assertEqual(tracker.get_rating_asof("CLUB", 3, t0 + timedelta(minutes=125))[1], 1)

        # Publicación tardía (t0+200m): NO disponible a t0+150m, sí a t0+200m
        self.assertEqual(tracker.get_rating_asof("CLUB", 5, t0 + timedelta(minutes=150))[1], 0)
        self.assertEqual(tracker.get_rating_asof("CLUB", 5, t0 + timedelta(minutes=200))[1], 1)

        # Prórroga AET: etiqueta explícitamente ESTIMATED y espera a +165m
        avail_aet, prov_aet = resolve_elo_available_dt({"ko_dt": t0, "status": "AET"})
        self.assertEqual(avail_aet, t0 + timedelta(minutes=165))
        self.assertTrue(prov_aet.startswith("ESTIMATED_"))
        self.assertEqual(tracker.get_rating_asof("CLUB", 7, t0 + timedelta(minutes=130))[1], 0)
        self.assertEqual(tracker.get_rating_asof("CLUB", 7, t0 + timedelta(minutes=165))[1], 1)

        # Interrumpidos nunca entran
        self.assertEqual(tracker.get_rating_asof("CLUB", 9, t0 + timedelta(days=7))[1], 0)
        self.assertEqual(tracker.get_rating_asof("CLUB", 11, t0 + timedelta(days=7))[1], 0)

    def test_cli_synthetic_end_to_end(self) -> None:
        payload, manifest_path, _ = self._load_sample_payload()
        future_ko = datetime.now(timezone.utc) + timedelta(hours=6)
        payload["fixture"]["date"] = future_ko.isoformat()
        payload["generated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        payload["context"]["target_fixture_full"] = {
            "response": [
                {
                    "fixture": {"id": payload["fixture"]["fixture_id"], "date": future_ko.isoformat(), "status": {"short": "NS"}},
                    "league": {"id": payload["fixture"]["league_id"], "season": payload["fixture"]["season"]},
                    "teams": {
                        "home": {"id": payload["fixture"]["home_id"]},
                        "away": {"id": payload["fixture"]["away_id"]},
                    },
                    "goals": {"home": None, "away": None},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            me_tmp = td_path / "match_engine.json"
            me_tmp.write_text(json.dumps(payload), encoding="utf-8")
            rec_dir = td_path / "records"

            proc_rec = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/prospective_tracker.py"),
                    "record",
                    "--match-engine",
                    str(me_tmp),
                    "--manifest",
                    str(manifest_path),
                    "--records-dir",
                    str(rec_dir),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            rec_summary = json.loads(proc_rec.stdout)
            rec_file = Path(rec_summary["record_path"])
            self.assertTrue(rec_file.is_file())
            self.assertEqual(rec_summary["temporal_integrity_status"], "PREMATCH_VERIFIED")

            proc_eval = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/prospective_tracker.py"),
                    "evaluate",
                    "--records-dir",
                    str(rec_dir),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            ev_summary = json.loads(proc_eval.stdout)
            self.assertEqual(ev_summary["pending_n"], 1)
            self.assertEqual(ev_summary["settled_paired_n"], 0)


if __name__ == "__main__":
    unittest.main()
