#!/usr/bin/env python3
"""Pruebas obligatorias Iteración 2.2C.1:
1. M0 y M0+Elo divergen con ratings as-of distintos y válidos.
2. M4 y M4+Elo divergen bajo las mismas condiciones.
3. SoS se aplica con historial de rivales válido.
4. Ausencia de datos Elo/SoS produce degradación explícita y rechazo como comparación completa de 4 modelos.
5. Ningún dato posterior al cutoff entra al modelo (Elo ni SoS).
6. Fixture incorrecto/ambiguo queda bloqueado.
7. JSON de marcador sin procedencia acreditada no obtiene VERIFIED_AUTHORITATIVE.
8. Dos registros simultáneos no sobrescriben el mismo archivo (creación exclusiva).
9. Las cuotas conservan su timestamp de captura y separan evaluación deportiva vs económica.
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.context.observation_store import HistoricalTeamMatch
from bet_engine.modeling.prospective_registry import (
    ProspectiveRegistryError,
    evaluate_prospective_records,
    extract_bwin_capture_utc,
    record_prospective_prediction,
    settle_prospective_record,
)

ROOT = Path(__file__).resolve().parent.parent


def _hm(fid: int, tid: int, oid: int, ko: datetime, hg: float, ag: float, venue: str = "HOME", stratum: str = "CLUB") -> HistoricalTeamMatch:
    return HistoricalTeamMatch(
        fixture_id=fid,
        team_id=tid,
        opponent_id=oid,
        kickoff_utc=ko.isoformat(),
        available_at_utc=(ko + timedelta(minutes=125)).isoformat(),
        league_id=39,
        league_name="Test League",
        season=2026,
        competition_stratum=stratum,
        venue=venue,
        status="FT",
        coach_id=None,
        coach_name=None,
        red_card_distortion=False,
        own_metrics={"goals": hg},
        conceded_metrics={"goals": ag},
        has_detailed_stats=True,
        source="TEST",
    )


class TestIteracion22C1Fixes(unittest.TestCase):
    def _load_real_future_payload(self) -> tuple[dict, Path, datetime]:
        me_path = ROOT / "outputs/matches/7905487/inputs/match_engine_7905487.json"
        manifest_path = ROOT / "audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json"
        if not (me_path.is_file() and manifest_path.is_file()):
            self.skipTest("Fixtures o manifest no presentes")
        payload = copy.deepcopy(json.loads(me_path.read_text(encoding="utf-8")))
        future_ko = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
        now_pre = future_ko - timedelta(hours=3)
        payload["fixture"]["date"] = future_ko.isoformat()
        payload["generated_at"] = (now_pre - timedelta(minutes=5)).isoformat()
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

    def test_1_2_3_4_5_elo_sos_divergence_degradation_and_cutoff_guard(self) -> None:
        payload, manifest_path, future_ko = self._load_real_future_payload()
        now_pre = future_ko - timedelta(hours=3)

        # Fixture post-cutoff que NO debe entrar nunca ni en Elo ni en SoS
        post_ko = now_pre + timedelta(minutes=10)
        hid = int(payload["fixture"]["home_id"])
        aid = int(payload["fixture"]["away_id"])
        extra_post_fx = [
            {"fixture_id": 999991, "home_id": hid, "away_id": 9991, "hg": 9, "ag": 0, "stratum": "NATIONAL_TEAM", "ko_dt": post_ko, "status": "FT"},
        ]
        extra_post_team = {
            9991: [_hm(999992, 9991, 9992, post_ko, 0, 8, stratum="NATIONAL_TEAM")],
        }

        with tempfile.TemporaryDirectory() as td:
            # A) Con corpus real as-of: las 4 variantes deben divergir y aplicar Elo + SoS
            rec_full_dir = Path(td) / "full"
            rec_full = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=rec_full_dir,
                corpus_root=ROOT,
                extra_fixtures=extra_post_fx,
                extra_team_series=extra_post_team,
                now_utc=now_pre,
            )
            doc = json.loads(rec_full.read_text(encoding="utf-8"))
            pre = doc["pre_kickoff"]
            preds = pre["predictions"]

            # Test 1: M0 y M0+Elo divergen con ratings Elo reales distintos
            self.assertTrue(preds["M0_plus_Elo"]["components"]["elo_coverage_ok"])
            self.assertNotEqual(preds["M0_plus_Elo"]["components"]["elo_home"], preds["M0_plus_Elo"]["components"]["elo_away"])
            self.assertNotEqual(preds["M0"]["outcomes_1x2"], preds["M0_plus_Elo"]["outcomes_1x2"])

            # Test 2: M4 y M4+Elo divergen bajo las mismas condiciones
            self.assertTrue(preds["M4_plus_Elo"]["components"]["elo_coverage_ok"])
            self.assertNotEqual(preds["M4"]["lambda_home"], preds["M4_plus_Elo"]["lambda_home"])
            self.assertNotEqual(preds["M4"]["outcomes_1x2"], preds["M4_plus_Elo"]["outcomes_1x2"])

            # Test 3: SoS se aplica con historial de rivales válido
            self.assertGreater(preds["M4"]["components"]["sos_coverage_rate"], 0.0)
            self.assertGreater(preds["M4_plus_Elo"]["components"]["sos_coverage_rate"], 0.0)
            self.assertTrue(pre["model_comparison_integrity"]["full_four_model_comparison_valid"])

            # Test 5: Ningún dato posterior al cutoff entró al modelo
            elo_meta = pre["provenance"]["elo_builder_meta"]
            self.assertGreaterEqual(elo_meta["excluded_post_cutoff_n"], 2)
            self.assertEqual(
                pre["timing"]["temporal_integrity_status"],
                "PREMATCH_VERIFIED",
            )

            # B) Test 4: Ausencia de corpus/rivales degrada explícitamente y se rechaza en evaluate
            rec_deg_dir = Path(td) / "degraded"
            # Forzar un equipo sin rivales en team_ordered_by_id pasando diccionario vacío
            rec_deg = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=rec_deg_dir,
                team_ordered_by_id={},
                now_utc=now_pre,
            )
            deg_pre = json.loads(rec_deg.read_text(encoding="utf-8"))["pre_kickoff"]
            mci_deg = deg_pre["model_comparison_integrity"]
            self.assertFalse(mci_deg["full_four_model_comparison_valid"])
            self.assertIn("M4_DEGRADED_NO_SOS_COVERAGE", mci_deg["degradation_reasons"])

            # Liquidar con fuente autorizada: evaluate debe rechazarlo como comparación completa de 4 modelos
            auth_ok = {
                "get": "fixtures",
                "parameters": {"id": str(deg_pre["fixture_id"])},
                "_provenance": {"provider": "API-Football", "captured_at_utc": (future_ko + timedelta(minutes=140)).isoformat()},
                "response": [
                    {
                        "fixture": {"id": deg_pre["fixture_id"], "status": {"short": "FT"}},
                        "league": {"id": deg_pre["competition"]["league_id"]},
                        "teams": {"home": {"id": hid}, "away": {"id": aid}},
                        "goals": {"home": 1, "away": 1},
                    }
                ],
            }
            settle_prospective_record(
                rec_deg,
                hg=1,
                ag=1,
                match_status="FT",
                settled_at_utc=future_ko + timedelta(minutes=140),
                authoritative_fixture=auth_ok,
            )
            ev_deg = evaluate_prospective_records(rec_deg_dir)
            self.assertEqual(ev_deg["settled_paired_n"], 0)
            self.assertEqual(ev_deg["degraded_comparison_rejected_n"], 1)

    def test_6_fixture_identity_and_unknown_status_blocked(self) -> None:
        payload, manifest_path, future_ko = self._load_real_future_payload()
        now_pre = future_ko - timedelta(hours=2)

        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "records"
            # Discrepancia de fixture_id
            bad_id = copy.deepcopy(payload)
            bad_id["context"]["target_fixture_full"]["response"][0]["fixture"]["id"] = 99999999
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(match_engine_payload=bad_id, manifest_path=manifest_path, records_dir=rec_dir, now_utc=now_pre)

            # Discrepancia de equipos
            bad_team = copy.deepcopy(payload)
            bad_team["context"]["target_fixture_full"]["response"][0]["teams"]["home"]["id"] = 123456
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(match_engine_payload=bad_team, manifest_path=manifest_path, records_dir=rec_dir, now_utc=now_pre)

            # Contradicción de kickoff
            bad_date = copy.deepcopy(payload)
            bad_date["context"]["target_fixture_full"]["response"][0]["fixture"]["date"] = (future_ko + timedelta(hours=2)).isoformat()
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(match_engine_payload=bad_date, manifest_path=manifest_path, records_dir=rec_dir, now_utc=now_pre)

            # Estado desconocido / ambiguo (TBD / UNKNOWN)
            for st in ("TBD", "UNKNOWN", ""):
                bad_st = copy.deepcopy(payload)
                bad_st["context"]["target_fixture_full"]["response"][0]["fixture"]["status"]["short"] = st
                with self.assertRaises(ProspectiveRegistryError):
                    record_prospective_prediction(match_engine_payload=bad_st, manifest_path=manifest_path, records_dir=rec_dir, now_utc=now_pre)

    def test_7_unaccredited_source_json_not_verified_authoritative(self) -> None:
        payload, manifest_path, future_ko = self._load_real_future_payload()
        now_pre = future_ko - timedelta(hours=2)

        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "records"
            rec_path = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=rec_dir,
                corpus_root=ROOT,
                now_utc=now_pre,
            )
            pre = json.loads(rec_path.read_text(encoding="utf-8"))["pre_kickoff"]

            # JSON con estructura correcta pero SIN procedencia verificada de proveedor
            arbitrary_json = {
                "response": [
                    {
                        "fixture": {"id": pre["fixture_id"], "status": {"short": "FT"}},
                        "league": {"id": pre["competition"]["league_id"]},
                        "teams": {"home": {"id": pre["teams"]["home_id"]}, "away": {"id": pre["teams"]["away_id"]}},
                        "goals": {"home": 2, "away": 0},
                    }
                ]
            }
            settled = settle_prospective_record(
                rec_path,
                hg=2,
                ag=0,
                match_status="FT",
                settled_at_utc=future_ko + timedelta(minutes=140),
                authoritative_fixture=arbitrary_json,
                source_path=Path("/tmp/arbitrary_manual.json"),
            )
            self.assertEqual(settled["settlement"]["verification_status"], "UNVERIFIED_SOURCE_PROVENANCE")
            self.assertFalse(settled["settlement"]["provider_provenance_verified"])
            self.assertFalse(settled["settlement"]["creditable"])
            ev = evaluate_prospective_records(rec_dir)
            self.assertEqual(ev["settled_paired_n"], 0)
            self.assertEqual(ev["unverified_result_n"], 1)

    def test_8_concurrent_records_exclusive_creation_no_overwrite(self) -> None:
        payload, manifest_path, future_ko = self._load_real_future_payload()
        now_pre = future_ko - timedelta(hours=2)

        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "concurrent"

            def _try_record() -> str:
                try:
                    p = record_prospective_prediction(
                        match_engine_payload=payload,
                        manifest_path=manifest_path,
                        records_dir=rec_dir,
                        now_utc=now_pre,
                    )
                    return f"OK:{p.name}"
                except ProspectiveRegistryError as exc:
                    return f"ERR:{exc}"

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                results = list(ex.map(lambda _: _try_record(), range(2)))

            ok_count = sum(1 for r in results if r.startswith("OK:"))
            err_count = sum(1 for r in results if "IMMUTABLE_RECORD_ALREADY_EXISTS" in r)
            self.assertEqual(ok_count, 1)
            self.assertEqual(err_count, 1)

    def test_9_bwin_odds_capture_timestamp_and_economic_separation(self) -> None:
        payload, manifest_path, future_ko = self._load_real_future_payload()
        now_pre = future_ko - timedelta(hours=2)

        with tempfile.TemporaryDirectory() as td:
            bwin_txt = Path(td) / "cuotas.txt"
            cap_iso = (now_pre - timedelta(minutes=15)).isoformat()
            bwin_txt.write_text(f"Evento: Gales vs Portugal\nCaptura: {cap_iso}\n1 2.50 X 3.10 2 2.80\n", encoding="utf-8")
            extracted = extract_bwin_capture_utc(bwin_txt)
            self.assertEqual(extracted, cap_iso)

            # Con captura válida -> ODDS_VERIFIED y elegible económicamente
            rec_ok = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=Path(td) / "rec_ok",
                corpus_root=ROOT,
                bwin_odds={"home_win": 2.50, "draw": 3.10, "away_win": 2.80},
                odds_captured_utc=extracted,
                odds_sha256="sha_ok",
                now_utc=now_pre,
            )
            pre_ok = json.loads(rec_ok.read_text(encoding="utf-8"))["pre_kickoff"]
            self.assertEqual(pre_ok["bwin_odds"]["odds_status"], "ODDS_VERIFIED")
            self.assertTrue(pre_ok["bwin_odds"]["economic_eval_eligible"])
            self.assertEqual(pre_ok["bwin_odds"]["captured_utc"], cap_iso)

            # Sin timestamp válido -> ODDS_UNVERIFIED, pero permite evaluar precisión deportiva
            rec_unv = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=Path(td) / "rec_unv",
                corpus_root=ROOT,
                bwin_odds={"home_win": 2.50, "draw": 3.10, "away_win": 2.80},
                odds_captured_utc=None,
                odds_sha256="sha_ok",
                now_utc=now_pre,
            )
            pre_unv = json.loads(rec_unv.read_text(encoding="utf-8"))["pre_kickoff"]
            self.assertEqual(pre_unv["bwin_odds"]["odds_status"], "ODDS_UNVERIFIED")
            self.assertFalse(pre_unv["bwin_odds"]["economic_eval_eligible"])

            auth_fx = {
                "get": "fixtures",
                "parameters": {"id": str(pre_unv["fixture_id"])},
                "_provenance": {"provider": "API-Football", "captured_at_utc": (future_ko + timedelta(minutes=140)).isoformat()},
                "response": [
                    {
                        "fixture": {"id": pre_unv["fixture_id"], "status": {"short": "FT"}},
                        "league": {"id": pre_unv["competition"]["league_id"]},
                        "teams": {"home": {"id": pre_unv["teams"]["home_id"]}, "away": {"id": pre_unv["teams"]["away_id"]}},
                        "goals": {"home": 1, "away": 0},
                    }
                ],
            }
            settle_prospective_record(
                rec_unv,
                hg=1,
                ag=0,
                match_status="FT",
                settled_at_utc=future_ko + timedelta(minutes=140),
                authoritative_fixture=auth_fx,
            )
            ev_unv = evaluate_prospective_records(Path(td) / "rec_unv")
            self.assertEqual(ev_unv["settled_paired_n"], 1)  # Precisión deportiva sí se evalúa
            self.assertEqual(ev_unv["economic_eval_eligible_n"], 0)  # Métrica económica NO


if __name__ == "__main__":
    unittest.main()
