#!/usr/bin/env python3
"""Pruebas Iteración 2.2B:
1. Trazabilidad temporal Elo (AET >=165m, PEN >=180m, exclusión no terminados, fallback +180m).
2. Registro prospectivo PREMATCH inmutable (anti-retrospectivo, alineaciones confirmadas vs probables,
   liquidación post-partido, detección de manipulación y evaluación pareada de 4 modelos).
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.modeling.experimental_v2_1b import AsOfEloTracker, resolve_elo_available_dt
from bet_engine.modeling.prospective_registry import (
    ProspectiveRegistryError,
    evaluate_prospective_records,
    record_prospective_prediction,
    settle_prospective_record,
)

ROOT = Path(__file__).resolve().parent.parent


class TestIteracion22BProspectiveAndElo(unittest.TestCase):
    def test_elo_aet_pen_non_finished_and_conservative_fallback(self) -> None:
        t0 = datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)
        fixtures = [
            # Prórroga AET: NO debe estar disponible a ko + 130m, sí a ko + 165m
            {"fixture_id": 1, "home_id": 10, "away_id": 20, "hg": 2, "ag": 1, "stratum": "CLUB", "ko_dt": t0, "status": "AET"},
            # Penaltis PEN: NO debe estar disponible a ko + 170m, sí a ko + 180m
            {"fixture_id": 2, "home_id": 30, "away_id": 40, "hg": 1, "ag": 1, "stratum": "CLUB", "ko_dt": t0, "status": "PEN"},
            # Suspendido / En vivo: DEBE excluirse completamente
            {"fixture_id": 3, "home_id": 50, "away_id": 60, "hg": 3, "ag": 0, "stratum": "CLUB", "ko_dt": t0, "status": "INT"},
            {"fixture_id": 4, "home_id": 70, "away_id": 80, "hg": 1, "ag": 0, "stratum": "CLUB", "ko_dt": t0, "status": "2H"},
            # Sin estado ni avail_dt explícito: fallback conservador +180m
            {"fixture_id": 5, "home_id": 90, "away_id": 91, "hg": 2, "ag": 0, "stratum": "CLUB", "ko_dt": t0},
        ]
        tracker = AsOfEloTracker.build_from_fixtures(fixtures)
        self.assertEqual(tracker.excluded_non_finished, 2)

        # A ko + 130m: AET todavía NO entró
        r10_130, n10_130 = tracker.get_rating_asof("CLUB", 10, t0 + timedelta(minutes=130))
        self.assertEqual((r10_130, n10_130), (1500.0, 0))
        # A ko + 165m: AET sí entró
        r10_165, n10_165 = tracker.get_rating_asof("CLUB", 10, t0 + timedelta(minutes=165))
        self.assertEqual(n10_165, 1)
        self.assertGreater(r10_165, 1500.0)

        # A ko + 170m: PEN y fallback desconocido todavía NO entraron
        self.assertEqual(tracker.get_rating_asof("CLUB", 30, t0 + timedelta(minutes=170))[1], 0)
        self.assertEqual(tracker.get_rating_asof("CLUB", 90, t0 + timedelta(minutes=170))[1], 0)
        # A ko + 180m: ambos entran
        self.assertEqual(tracker.get_rating_asof("CLUB", 30, t0 + timedelta(minutes=180))[1], 1)
        self.assertEqual(tracker.get_rating_asof("CLUB", 90, t0 + timedelta(minutes=180))[1], 1)

        # Suspendido/en vivo nunca entra ni mucho después
        self.assertEqual(tracker.get_rating_asof("CLUB", 50, t0 + timedelta(days=5))[1], 0)
        self.assertEqual(tracker.get_rating_asof("CLUB", 70, t0 + timedelta(days=5))[1], 0)

        # Disponibilidad explícita posterior retrasa el punto de disponibilidad
        avail_obs, prov_obs = resolve_elo_available_dt({
            "ko_dt": t0,
            "status": "FT",
            "observed_available_at_utc": (t0 + timedelta(minutes=210)).isoformat(),
        })
        self.assertEqual(avail_obs, t0 + timedelta(minutes=210))
        self.assertEqual(prov_obs, "VERIFIED_TIMESTAMP")

    def test_prospective_record_settle_tamper_and_paired_eval(self) -> None:
        me_path = ROOT / "outputs/matches/7905487/inputs/match_engine_7905487.json"
        manifest_path = ROOT / "audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json"
        if not (me_path.is_file() and manifest_path.is_file()):
            self.skipTest("Fixtures o manifest no presentes")

        base_payload = json.loads(me_path.read_text(encoding="utf-8"))
        future_ko = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
        now_pre = future_ko - timedelta(hours=3)
        payload = copy.deepcopy(base_payload)
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

        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "records"
            # 1. Bloquear registro retrospectivo si now_utc >= kickoff
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(
                    match_engine_payload=payload,
                    manifest_path=manifest_path,
                    records_dir=rec_dir,
                    now_utc=future_ko + timedelta(minutes=10),
                )
            # 2. Bloquear si status == FT
            ft_payload = copy.deepcopy(payload)
            ft_payload["context"]["target_fixture_full"]["response"][0]["fixture"]["status"]["short"] = "FT"
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(
                    match_engine_payload=ft_payload,
                    manifest_path=manifest_path,
                    records_dir=rec_dir,
                    now_utc=now_pre,
                )
            # 3. Registro válido antes del kickoff
            rec_path = record_prospective_prediction(
                match_engine_payload=payload,
                manifest_path=manifest_path,
                records_dir=rec_dir,
                corpus_root=ROOT,
                bwin_odds={"home_win": 2.35, "draw": 3.20, "away_win": 3.10},
                odds_captured_utc=now_pre.isoformat(),
                odds_sha256="bwin_sha_22b",
                now_utc=now_pre,
            )
            doc = json.loads(rec_path.read_text(encoding="utf-8"))
            pre = doc["pre_kickoff"]
            self.assertEqual(set(pre["predictions"].keys()), {"M0", "M0_plus_Elo", "M4", "M4_plus_Elo"})
            self.assertEqual(pre["timing"]["temporal_integrity_status"], "PREMATCH_VERIFIED")
            self.assertFalse(pre["lineup_status"]["confirmed_complete_22"])

            # 4. Inmutabilidad: no permite sobrescribir registro existente
            with self.assertRaises(ProspectiveRegistryError):
                record_prospective_prediction(
                    match_engine_payload=payload,
                    manifest_path=manifest_path,
                    records_dir=rec_dir,
                    now_utc=now_pre,
                )

            # 5. Rechaza liquidación prematura antes de ko + 125m
            with self.assertRaises(ProspectiveRegistryError):
                settle_prospective_record(rec_path, hg=2, ag=1, match_status="FT", settled_at_utc=future_ko + timedelta(minutes=90))

            # 6. Liquidación válida después de FT con fuente autorizada (sin alterar el registro original)
            auth_fx = {
                "get": "fixtures",
                "parameters": {"id": str(pre["fixture_id"])},
                "_provenance": {"provider": "API-Football", "captured_at_utc": (future_ko + timedelta(minutes=135)).isoformat()},
                "response": [
                    {
                        "fixture": {"id": pre["fixture_id"], "status": {"short": "FT"}},
                        "league": {"id": pre["competition"]["league_id"]},
                        "teams": {
                            "home": {"id": pre["teams"]["home_id"]},
                            "away": {"id": pre["teams"]["away_id"]},
                        },
                        "goals": {"home": 2, "away": 1},
                    }
                ],
            }
            settled = settle_prospective_record(
                rec_path,
                hg=2,
                ag=1,
                match_status="FT",
                settled_at_utc=future_ko + timedelta(minutes=140),
                authoritative_fixture=auth_fx,
            )
            self.assertEqual(settled["settlement"]["status"], "SETTLED")
            self.assertEqual(settled["settlement"]["verification_status"], "VERIFIED_AUTHORITATIVE")
            self.assertEqual(set(settled["settlement"]["model_scores"].keys()), {"M0", "M0_plus_Elo", "M4", "M4_plus_Elo"})

            # 7. Evaluación pareada sobre exactamente el mismo partido
            ev = evaluate_prospective_records(rec_dir)
            self.assertEqual(ev["settled_paired_n"], 1)
            self.assertFalse(ev["sample_sufficiency"]["CLUB"]["sufficient"])
            self.assertEqual(set(ev["ALL"]["models"].keys()), {"M0", "M0_plus_Elo", "M4", "M4_plus_Elo"})

            # 8. Detección de manipulación si se altera pre_kickoff
            tampered = json.loads(rec_path.read_text(encoding="utf-8"))
            tampered["pre_kickoff"]["predictions"]["M0"]["p_over_2_5"] = 0.99
            rec_path.write_text(json.dumps(tampered), encoding="utf-8")
            ev2 = evaluate_prospective_records(rec_dir)
            self.assertEqual(len(ev2["tampered_rejected"]), 1)


if __name__ == "__main__":
    unittest.main()
