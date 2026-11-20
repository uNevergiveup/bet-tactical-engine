#!/usr/bin/env python3
"""Pruebas de integración prospectiva y estabilización (Fase 2B.4).

Cubre los 8 bloques obligatorios de verificación:
1. Reproducibilidad exacta del baseline de producción (CoherentMatchModeler_baseline_2.0_2.1A).
2. Reproducibilidad exacta de CoherentMatchModeler_cand_2b3b_v1 respecto a run_fase2b3b_experiments / 2B.3A.
3. Evaluación paralela sobre el mismo bundle / payload (B0_baseline vs CAND_2B3B_V1) y diagnóstico de cobertura/odds/coaches.
4. Inmutabilidad temporal pre-kickoff y verificación de hashes del manifiesto MANIFEST_CANDIDATA_2B3B.json.
5. Liquidación separada, antifuga, procedencia autorizada vs simulada/no verificada y detección de manipulación.
6. Idempotencia y aislamiento ante fallos (batch, refresh en Shadow, fallo simulado del evaluador experimental).
7. Imposibilidad arquitectónica de autorizar apuestas (BET) desde CoherentMatchModeler_cand_2b3b_v1.
8. Compatibilidad con flujo PREMATCH/Shadow y CLI tools/prospective_tracker.py (--track 2b3b y --track legacy).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bet_engine.context.lineup_manager import LineupStatus
from bet_engine.context.observation_store import ObservationStore
from bet_engine.modeling.distributions import (
    DEFAULT_PRIORS,
    MODEL_VERSION,
    CoherentMatchModeler,
    _resolve_prior,
)
from bet_engine.modeling.prospective_cand_2b3b import (
    BASELINE_2B3B_KEY,
    CAND_2B3B_SPEC_PARAMS,
    CANDIDATE_2B3B_KEY,
    CANDIDATE_2B3B_VERSION,
    estimate_cand_2b3b_side_rate,
    evaluate_2b3b_prospective_records,
    evaluate_parallel_bundle_2b3b,
    record_2b3b_batch,
    record_2b3b_prospective_prediction,
    resolve_target_stratum,
    settle_2b3b_prospective_record,
    verify_cand_2b3b_manifest,
)
from bet_engine.modeling.prospective_registry import CORPUS_CUTOFF_UTC, ProspectiveRegistryError
from bet_engine.orchestration.adapters import run_shadow_for_event_dir
from bet_engine.orchestration.shadow_pipeline import ShadowPipelineRunner
from tools.run_fase2b3a_experiments import VariantSpec, _estimate_side_rate_variant

ROOT = Path(__file__).resolve().parent.parent
CAND_MANIFEST_PATH = ROOT / "audit" / "fase2b_experimentos" / "MANIFEST_CANDIDATA_2B3B.json"
V2_MANIFEST_PATH = ROOT / "audit" / "fase2b_backtesting" / "MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json"
SAMPLE_MATCH_ENGINE_PATH = ROOT / "outputs" / "matches" / "7909713" / "inputs" / "match_engine_7909713.json"
FALLBACK_MATCH_ENGINE_PATH = ROOT / "outputs" / "matches" / "7905487" / "inputs" / "match_engine_7905487.json"


def _load_future_payload(
    *,
    fixture_id_offset: int = 0,
    event_id_override: str | None = None,
    kickoff_dt: datetime | None = None,
    cutoff_dt: datetime | None = None,
    keep_only_home_away_count: int | None = None,
) -> tuple[dict, datetime, datetime]:
    src_path = SAMPLE_MATCH_ENGINE_PATH if SAMPLE_MATCH_ENGINE_PATH.is_file() else FALLBACK_MATCH_ENGINE_PATH
    if not src_path.is_file():
        raise unittest.SkipTest("No hay match_engine fixture disponible para construir la prueba")
    payload = copy.deepcopy(json.loads(src_path.read_text(encoding="utf-8")))
    ko_dt = kickoff_dt or datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
    cut_dt = cutoff_dt or (ko_dt - timedelta(hours=3))

    if fixture_id_offset:
        payload["fixture"]["fixture_id"] = int(payload["fixture"]["fixture_id"]) + fixture_id_offset
    if event_id_override is not None:
        payload.setdefault("match", {})["event_id"] = event_id_override

    payload["generated_at"] = cut_dt.isoformat()
    payload["fixture"]["date"] = ko_dt.isoformat()
    payload["fixture"]["status"] = "NS"
    ctx = payload.setdefault("context", {})
    ctx["target_fixture_full"] = {
        "response": [
            {
                "fixture": {
                    "id": payload["fixture"]["fixture_id"],
                    "date": ko_dt.isoformat(),
                    "status": {"short": "NS"},
                },
                "league": {
                    "id": payload["fixture"]["league_id"],
                    "name": payload["fixture"].get("league_name", "Test League"),
                    "season": payload["fixture"]["season"],
                },
                "teams": {
                    "home": {"id": payload["fixture"]["home_id"], "name": payload["fixture"]["home_name"]},
                    "away": {"id": payload["fixture"]["away_id"], "name": payload["fixture"]["away_name"]},
                },
                "goals": {"home": None, "away": None},
            }
        ]
    }
    if keep_only_home_away_count is not None:
        for team_key in ("home", "away"):
            tctx = ctx.get(team_key) or {}
            if "last_n_history" in tctx and isinstance(tctx["last_n_history"], list):
                tctx["last_n_history"] = tctx["last_n_history"][:keep_only_home_away_count]
    return payload, ko_dt, cut_dt


def _make_auth_fixture(pre: dict, hg: int, ag: int, *, status: str = "FT") -> dict:
    return {
        "get": "fixtures",
        "parameters": {"id": str(pre["fixture_id"])},
        "_provenance": {"provider": "API-Football", "captured_at_utc": "2026-11-26T00:10:00+00:00"},
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


def _write_temp_event_dir(root: Path, payload: dict, *, event_id: str, include_valid_odds: bool = True) -> Path:
    ev_dir = root / "outputs" / "matches" / str(event_id)
    (ev_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (ev_dir / "instructions").mkdir(parents=True, exist_ok=True)

    me_path = ev_dir / "inputs" / f"match_engine_{event_id}.json"
    me_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    inputs_meta: dict[str, dict] = {
        "context_json": {"path": str(me_path), "sha256": hashlib.sha256(me_path.read_bytes()).hexdigest()}
    }

    if include_valid_odds:
        fx = payload["fixture"]
        ko_dt = datetime.fromisoformat(fx["date"].replace("Z", "+00:00"))
        cap_dt = ko_dt - timedelta(minutes=75)
        bwin_txt = (
            "============================================================\n"
            "BWIN - CUOTAS\n"
            f"Partido: {fx['home_name']} {fx['away_name']}\n"
            f"Event ID: {event_id}\n"
            f"Captura: {cap_dt.isoformat()}\n\n"
            "MERCADO: Resultado del partido | Tiempo reglamentario\n"
            f"{fx['home_name']} : 2.10\n"
            "X : 3.30\n"
            f"{fx['away_name']} : 3.50\n\n"
            "MERCADO: Total de goles | Tiempo reglamentario\n"
            "Más de 2,5 : 2.05\n"
            "Menos de 2,5 : 1.80\n\n"
            "MERCADO: Ambos equipos marcarán\n"
            "Sí : 1.90\n"
            "No : 1.90\n"
        )
        bwin_path = ev_dir / "inputs" / f"cuotas_bwin_{event_id}.txt"
        bwin_path.write_text(bwin_txt, encoding="utf-8")
        inputs_meta["odds"] = {"path": str(bwin_path), "sha256": hashlib.sha256(bwin_path.read_bytes()).hexdigest()}

    manifest = {
        "event_id": str(event_id),
        "fixture_id": payload["fixture"]["fixture_id"],
        "inputs": inputs_meta,
    }
    (ev_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return ev_dir


class TestFase2B4ProspectiveIntegration(unittest.TestCase):
    def test_1_baseline_production_parity_and_untouched_priors(self) -> None:
        self.assertEqual(DEFAULT_PRIORS["goals"], (1.56, 1.26))
        self.assertEqual(MODEL_VERSION, "CoherentMatchModeler_baseline_2.0_2.1A")

        payload, _, cut_dt = _load_future_payload()
        bundle = ObservationStore.from_match_engine_json(payload, cutoff_utc=cut_dt)
        b0_direct = CoherentMatchModeler.build_model(bundle, history_limit=20)
        par = evaluate_parallel_bundle_2b3b(bundle, history_limit=20)

        b0_rec = par[BASELINE_2B3B_KEY]
        self.assertEqual(par["baseline_version"], "CoherentMatchModeler_baseline_2.0_2.1A")
        self.assertAlmostEqual(b0_rec["lambda_home"], float(b0_direct.families["GOALS_FT"].lambda_home), places=9)
        self.assertAlmostEqual(b0_rec["lambda_away"], float(b0_direct.families["GOALS_FT"].lambda_away), places=9)
        self.assertAlmostEqual(b0_rec["lambda_total"], float(b0_direct.families["GOALS_FT"].lambda_total), places=9)
        self.assertEqual(b0_rec["outcomes_1x2"]["home_win"], round(float(b0_direct.outcomes_1x2["home_win"]), 6))
        self.assertEqual(b0_rec["outcomes_1x2"]["draw"], round(float(b0_direct.outcomes_1x2["draw"]), 6))
        self.assertEqual(b0_rec["outcomes_1x2"]["away_win"], round(float(b0_direct.outcomes_1x2["away_win"]), 6))
        self.assertEqual(
            b0_rec["p_over_2_5"],
            round(float(b0_direct.families["GOALS_FT"].prob_over("total", 2.5)["p_over"]), 6),
        )
        self.assertEqual(b0_rec["p_btts_yes"], round(float(b0_direct.btts["yes"]), 6))

    def test_2_candidate_cand_2b3b_v1_exact_reproduction_of_frozen_spec(self) -> None:
        self.assertEqual(CANDIDATE_2B3B_VERSION, "CoherentMatchModeler_cand_2b3b_v1")
        self.assertEqual(CAND_2B3B_SPEC_PARAMS["spec_id"], "INT_B3_A6a_kish_k8_C4")
        self.assertEqual(CAND_2B3B_SPEC_PARAMS["shrink_k"], 8.0)
        self.assertTrue(CAND_2B3B_SPEC_PARAMS["use_kish_neff"])
        self.assertEqual(CAND_2B3B_SPEC_PARAMS["venue_blend"], 0.50)
        self.assertEqual(CAND_2B3B_SPEC_PARAMS["venue_k"], 4.0)
        self.assertFalse(CAND_2B3B_SPEC_PARAMS["bet_authorization_allowed"])

        frozen_spec = VariantSpec(
            variant_id="INT_B3_A6a_kish_k8_C4",
            group="2B3B",
            description="CoherentMatchModeler_cand_2b3b_v1",
            shrink_k=8.0,
            use_kish_neff=True,
            venue_mode="shrunk_anchor_50",
            venue_blend=0.50,
            venue_k=4.0,
        )

        payload, _, cut_dt = _load_future_payload()
        bundle = ObservationStore.from_match_engine_json(payload, cutoff_utc=cut_dt)
        stratum = resolve_target_stratum(bundle)
        hp = _resolve_prior("goals", stratum, "HOME")
        ap = _resolve_prior("goals", stratum, "AWAY")

        cand_h = estimate_cand_2b3b_side_rate(
            bundle.home_matches,
            bundle.away_matches,
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum=stratum,
            own_venue="HOME",
            opp_venue="AWAY",
            prior_mean=hp,
            other_venue_prior_mean=ap,
            history_limit=20,
        )
        ref_h = _estimate_side_rate_variant(
            bundle.home_matches,
            bundle.away_matches,
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum=stratum,
            own_venue="HOME",
            opp_venue="AWAY",
            prior_mean=hp,
            other_venue_prior_mean=ap,
            history_limit=20,
            spec=frozen_spec,
        )
        cand_a = estimate_cand_2b3b_side_rate(
            bundle.away_matches,
            bundle.home_matches,
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum=stratum,
            own_venue="AWAY",
            opp_venue="HOME",
            prior_mean=ap,
            other_venue_prior_mean=hp,
            history_limit=20,
        )
        ref_a = _estimate_side_rate_variant(
            bundle.away_matches,
            bundle.home_matches,
            target_league_id=bundle.league_id,
            target_season=bundle.season,
            target_stratum=stratum,
            own_venue="AWAY",
            opp_venue="HOME",
            prior_mean=ap,
            other_venue_prior_mean=hp,
            history_limit=20,
            spec=frozen_spec,
        )

        self.assertLess(abs(cand_h["lambda"] - ref_h["lambda"]), 1e-12)
        self.assertLess(abs(cand_h["n_eff"] - ref_h["n_eff"]), 1e-12)
        self.assertLess(abs(cand_a["lambda"] - ref_a["lambda"]), 1e-12)
        self.assertLess(abs(cand_a["n_eff"] - ref_a["n_eff"]), 1e-12)

    def test_3_parallel_evaluation_identical_inputs_and_odds_classification(self) -> None:
        payload, ko_dt, cut_dt = _load_future_payload()
        now_pre = ko_dt - timedelta(hours=1)
        valid_odds = {"1x2_home": 2.05, "1x2_draw": 3.40, "1x2_away": 3.60, "over_2_5": 1.95}
        odds_cap = (now_pre - timedelta(minutes=15)).isoformat()
        odds_sha = hashlib.sha256(b"bwin-snapshot-bytes").hexdigest()

        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td) / "records"
            p_ver = record_2b3b_prospective_prediction(
                match_engine_payload=payload,
                records_dir=rec_dir,
                manifest_path=CAND_MANIFEST_PATH,
                bwin_odds=valid_odds,
                odds_captured_utc=odds_cap,
                odds_sha256=odds_sha,
                shadow_decision="NO_BET",
                now_utc=now_pre,
                verify_manifest=True,
            )
            doc_ver = json.loads(p_ver.read_text(encoding="utf-8"))["pre_kickoff"]
            self.assertEqual(doc_ver["bwin_odds"]["odds_status"], "ODDS_VERIFIED")
            self.assertTrue(doc_ver["bwin_odds"]["economic_eval_eligible"])
            self.assertIn("observations_sha256", doc_ver["provenance"])
            self.assertIn("insufficient_venue_any_lt3", doc_ver["diagnostics"]["history_coverage"])
            self.assertIn("insufficient_own_home_lt3", doc_ver["diagnostics"]["history_coverage"])
            self.assertIn("analytical_quality_score", doc_ver["diagnostics"]["data_quality"])
            self.assertEqual(doc_ver["predictions"][BASELINE_2B3B_KEY]["role"], "PRODUCTION_BASELINE")
            self.assertEqual(doc_ver["predictions"][CANDIDATE_2B3B_KEY]["role"], "EXPERIMENTAL_CANDIDATE_ONLY")

            # Sin timestamp/sha de captura Bwin -> ODDS_UNVERIFIED y excluido de eval económica
            payload2, ko2, _ = _load_future_payload(fixture_id_offset=901, event_id_override="9000901")
            p_unv = record_2b3b_prospective_prediction(
                match_engine_payload=payload2,
                records_dir=rec_dir,
                manifest_path=CAND_MANIFEST_PATH,
                bwin_odds=valid_odds,
                odds_captured_utc=None,
                odds_sha256=None,
                now_utc=ko2 - timedelta(hours=1),
            )
            doc_unv = json.loads(p_unv.read_text(encoding="utf-8"))["pre_kickoff"]
            self.assertEqual(doc_unv["bwin_odds"]["odds_status"], "ODDS_UNVERIFIED")
            self.assertFalse(doc_unv["bwin_odds"]["economic_eval_eligible"])
            self.assertEqual(doc_unv["bwin_odds"]["quotes"], {})
            self.assertIn("BWIN_ODDS_UNVERIFIED_TIMESTAMP", doc_unv["exclusion_reasons"])

    def test_4_temporal_immutability_and_manifest_hash_integrity(self) -> None:
        mv = verify_cand_2b3b_manifest(CAND_MANIFEST_PATH)
        self.assertEqual(mv["manifest"]["candidate_version"], CANDIDATE_2B3B_VERSION)
        self.assertIn("bet_engine/modeling/prospective_cand_2b3b.py", mv["verified_hashes"])

        payload, ko_dt, _ = _load_future_payload(fixture_id_offset=1001, event_id_override="9001001")
        now_pre = ko_dt - timedelta(hours=2)

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            rec_dir = td_p / "records"

            # Manifiesto alterado debe fallar
            bad_manifest = json.loads(CAND_MANIFEST_PATH.read_text(encoding="utf-8"))
            bad_manifest["code_hashes"]["bet_engine/modeling/prospective_cand_2b3b.py"] = "0" * 64
            bad_m_path = td_p / "BAD_MANIFEST.json"
            bad_m_path.write_text(json.dumps(bad_manifest), encoding="utf-8")
            with self.assertRaises(ProspectiveRegistryError):
                verify_cand_2b3b_manifest(bad_m_path)
            with self.assertRaises(ProspectiveRegistryError):
                record_2b3b_prospective_prediction(
                    match_engine_payload=payload,
                    records_dir=rec_dir,
                    manifest_path=bad_m_path,
                    now_utc=now_pre,
                )

            # Bloqueos temporales obligatorios
            with self.assertRaises(ProspectiveRegistryError):
                record_2b3b_prospective_prediction(
                    match_engine_payload=payload, records_dir=rec_dir, now_utc=ko_dt + timedelta(seconds=1)
                )
            with self.assertRaises(ProspectiveRegistryError):
                record_2b3b_prospective_prediction(
                    match_engine_payload=payload, records_dir=rec_dir, allow_overwrite=True, now_utc=now_pre
                )

            past_ko = datetime.fromisoformat(CORPUS_CUTOFF_UTC.replace("Z", "+00:00"))
            past_payload, _, past_cut = _load_future_payload(
                fixture_id_offset=1002, kickoff_dt=past_ko, cutoff_dt=past_ko - timedelta(hours=2)
            )
            with self.assertRaises(ProspectiveRegistryError):
                record_2b3b_prospective_prediction(
                    match_engine_payload=past_payload, records_dir=rec_dir, now_utc=past_cut + timedelta(minutes=10)
                )

            # Escritura inmutable "xb" y bloqueo de duplicado bajo otro event_id para el mismo fixture_id
            out_p = record_2b3b_prospective_prediction(
                match_engine_payload=payload,
                records_dir=rec_dir,
                manifest_path=CAND_MANIFEST_PATH,
                now_utc=now_pre,
            )
            self.assertTrue(out_p.is_file())
            payload_dup_event = copy.deepcopy(payload)
            payload_dup_event["match"]["event_id"] = "9999999"
            with self.assertRaises(ProspectiveRegistryError):
                record_2b3b_prospective_prediction(
                    match_engine_payload=payload_dup_event,
                    records_dir=rec_dir,
                    manifest_path=CAND_MANIFEST_PATH,
                    now_utc=now_pre,
                )

    def test_5_separate_settlement_provenance_and_tamper_detection(self) -> None:
        base_ko = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
        rec_paths: list[Path] = []
        auth_fixtures: list[dict] = []

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            rec_dir = td_p / "records"
            set_dir = td_p / "settlements"

            # 6 partidos acreditados distribuidos en 6 días distintos -> activa DAILY_BLOCK_BOOTSTRAP
            scores = [(2, 1), (1, 1), (0, 2), (3, 1), (1, 0), (2, 2)]
            for idx, (hg, ag) in enumerate(scores):
                ko = base_ko + timedelta(days=idx)
                cut = ko - timedelta(hours=3)
                payload, _, _ = _load_future_payload(
                    fixture_id_offset=2000 + idx,
                    event_id_override=f"80020{idx}",
                    kickoff_dt=ko,
                    cutoff_dt=cut,
                )
                p = record_2b3b_prospective_prediction(
                    match_engine_payload=payload,
                    records_dir=rec_dir,
                    manifest_path=CAND_MANIFEST_PATH,
                    now_utc=ko - timedelta(hours=1),
                )
                rec_paths.append(p)
                pre = json.loads(p.read_text(encoding="utf-8"))["pre_kickoff"]
                auth_fixtures.append((pre, hg, ag, ko))

            # Liquidación prematura (< ko + 125m) y status no final deben rechazarse
            with self.assertRaises(ProspectiveRegistryError):
                settle_2b3b_prospective_record(
                    rec_paths[0],
                    hg=2,
                    ag=1,
                    settled_at_utc=auth_fixtures[0][3] + timedelta(minutes=60),
                    authoritative_fixture=_make_auth_fixture(auth_fixtures[0][0], 2, 1),
                    settlements_dir=set_dir,
                )
            with self.assertRaises(ProspectiveRegistryError):
                settle_2b3b_prospective_record(
                    rec_paths[0],
                    hg=2,
                    ag=1,
                    match_status="1H",
                    settled_at_utc=auth_fixtures[0][3] + timedelta(hours=3),
                    settlements_dir=set_dir,
                )

            # Verificar inmutabilidad byte-a-byte del registro pre_kickoff al liquidar
            before_bytes = [p.read_bytes() for p in rec_paths]
            for p, (pre, hg, ag, ko) in zip(rec_paths, auth_fixtures):
                src_json = td_p / f"auth_{pre['fixture_id']}.json"
                fx_doc = _make_auth_fixture(pre, hg, ag)
                src_json.write_text(json.dumps(fx_doc), encoding="utf-8")
                srep = settle_2b3b_prospective_record(
                    p,
                    hg=hg,
                    ag=ag,
                    match_status="FT",
                    settled_at_utc=ko + timedelta(hours=3),
                    authoritative_fixture=fx_doc,
                    source_path=src_json,
                    settlements_dir=set_dir,
                )
                self.assertEqual(srep["settlement"]["verification_status"], "VERIFIED_AUTHORITATIVE")
                self.assertTrue(srep["settlement"]["creditable"])

            for p, raw in zip(rec_paths, before_bytes):
                self.assertEqual(p.read_bytes(), raw)

            # Doble liquidación rechazada
            with self.assertRaises(ProspectiveRegistryError):
                settle_2b3b_prospective_record(
                    rec_paths[0], hg=2, ag=1, simulation_mode=True, settlements_dir=set_dir,
                    settled_at_utc=auth_fixtures[0][3] + timedelta(hours=3),
                )

            # Añadir 1 simulado y 1 no verificado -> NO deben entrar en settled_paired_n
            p_sim = record_2b3b_prospective_prediction(
                match_engine_payload=_load_future_payload(fixture_id_offset=2090, event_id_override="8002090", kickoff_dt=base_ko + timedelta(days=7))[0],
                records_dir=rec_dir,
                now_utc=base_ko + timedelta(days=7) - timedelta(hours=1),
            )
            settle_2b3b_prospective_record(
                p_sim, hg=1, ag=0, simulation_mode=True, settled_at_utc=base_ko + timedelta(days=7, hours=3), settlements_dir=set_dir
            )
            p_unv = record_2b3b_prospective_prediction(
                match_engine_payload=_load_future_payload(fixture_id_offset=2091, event_id_override="8002091", kickoff_dt=base_ko + timedelta(days=8))[0],
                records_dir=rec_dir,
                now_utc=base_ko + timedelta(days=8) - timedelta(hours=1),
            )
            settle_2b3b_prospective_record(
                p_unv, hg=0, ag=0, simulation_mode=False, settled_at_utc=base_ko + timedelta(days=8, hours=3), settlements_dir=set_dir
            )

            ev = evaluate_2b3b_prospective_records(rec_dir, settlements_dir=set_dir, bootstrap_replicates=200)
            self.assertEqual(ev["settled_paired_n"], 6)
            self.assertEqual(ev["simulated_test_n"], 1)
            self.assertEqual(ev["unverified_result_n"], 1)
            self.assertEqual(ev["accreditation_status"], "MUESTRA_INSUFICIENTE")
            self.assertFalse(ev["production_calibrated_out_of_sample"])
            self.assertTrue(ev["auto_promotion_disabled"])
            self.assertEqual(ev["ALL"]["paired_delta_cand_minus_b0"]["rps"]["bootstrap_mode"], "DAILY_BLOCK_BOOTSTRAP")
            self.assertIn("bonferroni_m4_p", ev["ALL"]["paired_delta_cand_minus_b0"]["rps"])
            self.assertIn("by_league", ev)
            self.assertIn("by_depth_bucket", ev)
            self.assertIn("by_quality_tier", ev)
            self.assertIn("by_lineup_state", ev)
            self.assertIn("INSUFFICIENT_VENUE_ANY_LT3", ev)
            self.assertIn("INSUFFICIENT_OWN_HOME_LT3", ev)

            # Detección de manipulación si se altera el registro pre_kickoff
            tampered_doc = json.loads(rec_paths[0].read_text(encoding="utf-8"))
            tampered_doc["pre_kickoff"]["predictions"][CANDIDATE_2B3B_KEY]["lambda_home"] = 9.99
            rec_paths[0].write_text(json.dumps(tampered_doc), encoding="utf-8")
            ev_tampered = evaluate_2b3b_prospective_records(rec_dir, settlements_dir=set_dir, bootstrap_replicates=100)
            self.assertIn(rec_paths[0].name, ev_tampered["tampered_rejected"])
            self.assertEqual(ev_tampered["settled_paired_n"], 5)

    def test_6_idempotency_batch_fault_isolation_and_shadow_non_blocking(self) -> None:
        payload_ok, ko_ok, _ = _load_future_payload(fixture_id_offset=3001, event_id_override="8003001")
        payload_bad_ko, past_ko, _ = _load_future_payload(
            fixture_id_offset=3002,
            event_id_override="8003002",
            kickoff_dt=datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc),
            cutoff_dt=datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc),
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            matches_root = root / "outputs" / "matches"
            records_dir = root / "outputs" / "prospective_2b3b"

            _write_temp_event_dir(root, payload_ok, event_id="8003001")
            _write_temp_event_dir(root, payload_bad_ko, event_id="8003002")
            # Tercero con JSON roto para probar aislamiento ante fallos
            bad_dir = matches_root / "8003003" / "inputs"
            bad_dir.mkdir(parents=True, exist_ok=True)
            (bad_dir / "match_engine_8003003.json").write_text("{corrupt json", encoding="utf-8")

            r1 = record_2b3b_batch(
                matches_root=matches_root,
                records_dir=records_dir,
                manifest_path=CAND_MANIFEST_PATH,
                now_utc=ko_ok - timedelta(hours=1),
                verify_manifest=True,
            )
            self.assertEqual(r1["recorded_count"], 1)
            self.assertGreaterEqual(len(r1["insufficient_or_blocked"]), 1)
            self.assertGreaterEqual(len(r1["capture_failures"]), 1)

            r2 = record_2b3b_batch(
                matches_root=matches_root,
                records_dir=records_dir,
                manifest_path=CAND_MANIFEST_PATH,
                now_utc=ko_ok - timedelta(hours=1),
                verify_manifest=True,
            )
            self.assertEqual(r2["recorded_count"], 0)
            self.assertEqual(r2["skipped_already_recorded_count"], 1)

            # Shadow refresh twice on event dir: preserved cleanly without breaking Shadow
            sh1 = run_shadow_for_event_dir(root, "8003001", now_utc=ko_ok - timedelta(minutes=70))
            self.assertTrue(sh1["ok"])
            sdoc1 = json.loads((matches_root / "8003001" / "shadow" / "shadow_summary_8003001.json").read_text(encoding="utf-8"))
            self.assertEqual(sdoc1["prospective_2b3b_status"]["status"], "ALREADY_RECORDED_IMMUTABLE_PRESERVED")

            # Nuevo evento en otro root -> Shadow registra RECORDED_IMMUTABLE en corrida 1 y PRESERVED en corrida 2
            with tempfile.TemporaryDirectory() as td2:
                root2 = Path(td2)
                _write_temp_event_dir(root2, payload_ok, event_id="8003001")
                s_a = run_shadow_for_event_dir(root2, "8003001", now_utc=ko_ok - timedelta(minutes=70))
                self.assertTrue(s_a["ok"])
                doc_a = json.loads((root2 / "outputs/matches/8003001/shadow/shadow_summary_8003001.json").read_text(encoding="utf-8"))
                self.assertEqual(doc_a["prospective_2b3b_status"]["status"], "RECORDED_IMMUTABLE")

                s_b = run_shadow_for_event_dir(root2, "8003001", now_utc=ko_ok - timedelta(minutes=65))
                self.assertTrue(s_b["ok"])
                doc_b = json.loads((root2 / "outputs/matches/8003001/shadow/shadow_summary_8003001.json").read_text(encoding="utf-8"))
                self.assertEqual(doc_b["prospective_2b3b_status"]["status"], "ALREADY_RECORDED_IMMUTABLE_PRESERVED")

    def test_7_cand_2b3b_cannot_authorize_bet_and_failure_leaves_shadow_identical(self) -> None:
        payload, _, cut_dt = _load_future_payload()
        bundle = ObservationStore.from_match_engine_json(payload, cutoff_utc=cut_dt)
        confirmed_lu = {
            "status": LineupStatus.LINEUPS_API_CONFIRMED.value,
            "confirmed_complete_22": True,
            "home_count": 11,
            "away_count": 11,
        }
        # Cuotas artificialmente extremas para demostrar que aunque GOALS_FT esté acreditado,
        # el candidato experimental 2B.3B NUNCA modifica las probabilidades usadas por Shadow/BET.
        odds_map = {"1X2|home": 4.50, "1X2|draw": 3.40, "1X2|away": 1.85, "OU|goals_total|2.5|over": 3.20, "OU|goals_total|2.5|under": 1.35}
        runner = ShadowPipelineRunner(min_lead_minutes=45)

        rep_ok = runner.run_shadow_on_bundle(
            bundle,
            odds_map=odds_map,
            lineup_evaluation=confirmed_lu,
            calibration_accredited_families={"GOALS_FT"},
            simulate_cand_2b3b_failure=False,
        )
        rep_fail = runner.run_shadow_on_bundle(
            bundle,
            odds_map=odds_map,
            lineup_evaluation=confirmed_lu,
            calibration_accredited_families={"GOALS_FT"},
            simulate_cand_2b3b_failure=True,
        )

        # Shadow sigue exactamente igual cuando falla 2B.3B, y cuando tiene éxito tiene flags de bloqueo BET
        self.assertTrue(rep_ok.experimental_cand_2b3b_comparison["experimental_only"])
        self.assertTrue(rep_ok.experimental_cand_2b3b_comparison["candidate_cannot_authorize_bet"])
        self.assertFalse(rep_ok.experimental_cand_2b3b_comparison[CANDIDATE_2B3B_KEY]["bet_authorization_allowed"])
        self.assertEqual(rep_fail.status, "DEGRADED_SHADOW")
        self.assertEqual(rep_fail.experimental_cand_2b3b_comparison["status"], "FAILED_ISOLATED_NON_BLOCKING")
        self.assertEqual(rep_ok.decision, rep_fail.decision)
        self.assertEqual(rep_ok.decision_taxonomy, rep_fail.decision_taxonomy)
        self.assertEqual(rep_ok.decision_classification, rep_fail.decision_classification)
        self.assertEqual(rep_ok.market_comparisons, rep_fail.market_comparisons)
        self.assertEqual(rep_ok.top_investigable_candidates, rep_fail.top_investigable_candidates)

        # Incluso si evaluate_parallel_bundle_2b3b devolviera probabilidades extremas p=0.99, no altera market_comparisons
        fake_extreme = copy.deepcopy(rep_ok.experimental_cand_2b3b_comparison)
        fake_extreme[CANDIDATE_2B3B_KEY]["outcomes_1x2"] = {"home_win": 0.99, "draw": 0.005, "away_win": 0.005}
        fake_extreme[CANDIDATE_2B3B_KEY]["p_over_2_5"] = 0.99
        with patch("bet_engine.modeling.prospective_cand_2b3b.evaluate_parallel_bundle_2b3b", return_value=fake_extreme):
            rep_mock = runner.run_shadow_on_bundle(
                bundle,
                odds_map=odds_map,
                lineup_evaluation=confirmed_lu,
                calibration_accredited_families={"GOALS_FT"},
            )
        self.assertEqual(rep_mock.decision, rep_ok.decision)
        self.assertEqual(rep_mock.market_comparisons, rep_ok.market_comparisons)
        self.assertEqual(rep_mock.top_investigable_candidates, rep_ok.top_investigable_candidates)

    def test_8_cli_track_2b3b_and_legacy_compatibility(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        now_live = datetime.now(timezone.utc)
        payload, _, _ = _load_future_payload(
            fixture_id_offset=4001,
            event_id_override="8004001",
            kickoff_dt=now_live + timedelta(hours=4),
            cutoff_dt=now_live - timedelta(minutes=30),
        )
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            me_path = td_p / "match_engine.json"
            me_path.write_text(json.dumps(payload), encoding="utf-8")
            rec_dir = td_p / "rec_2b3b"

            # --track 2b3b record
            p1 = subprocess.run(
                [
                    sys.executable,
                    "tools/prospective_tracker.py",
                    "--track",
                    "2b3b",
                    "record",
                    "--match-engine",
                    str(me_path),
                    "--manifest",
                    str(CAND_MANIFEST_PATH),
                    "--records-dir",
                    str(rec_dir),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(p1.returncode, 0, p1.stderr + p1.stdout)
            out1 = json.loads(p1.stdout)
            self.assertEqual(out1["track"], "2b3b")
            self.assertFalse(out1["experimental_bet_authorized"])
            rec_file = Path(out1["record_path"])

            # Segundo registro con kickoff hace 4h (> CORPUS_CUTOFF_UTC y ya terminado) para probar CLI settle --simulate
            past_ko = now_live - timedelta(hours=4)
            past_payload, _, _ = _load_future_payload(
                fixture_id_offset=4003,
                event_id_override="8004003",
                kickoff_dt=past_ko,
                cutoff_dt=past_ko - timedelta(hours=2),
            )
            rec_finished = record_2b3b_prospective_prediction(
                match_engine_payload=past_payload,
                records_dir=rec_dir,
                manifest_path=CAND_MANIFEST_PATH,
                now_utc=past_ko - timedelta(hours=1),
            )
            p_set = subprocess.run(
                [
                    sys.executable,
                    "tools/prospective_tracker.py",
                    "--track",
                    "2b3b",
                    "settle",
                    "--record",
                    str(rec_finished),
                    "--hg",
                    "2",
                    "--ag",
                    "1",
                    "--status",
                    "FT",
                    "--simulate",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(p_set.returncode, 0, p_set.stderr + p_set.stdout)
            set_doc = json.loads(p_set.stdout)
            self.assertEqual(set_doc["settlement"]["verification_status"], "SIMULATED_TEST_ONLY")
            self.assertFalse(set_doc["settlement"]["creditable"])

            # --track 2b3b evaluate
            p_eval = subprocess.run(
                [
                    sys.executable,
                    "tools/prospective_tracker.py",
                    "--track",
                    "2b3b",
                    "evaluate",
                    "--records-dir",
                    str(rec_dir),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(p_eval.returncode, 0, p_eval.stderr + p_eval.stdout)
            eval_doc = json.loads(p_eval.stdout)
            self.assertEqual(eval_doc["pending_n"], 1)
            self.assertEqual(eval_doc["simulated_test_n"], 1)
            self.assertEqual(eval_doc["settled_paired_n"], 0)
            self.assertFalse(eval_doc["production_calibrated_out_of_sample"])

            # --track 2b3b record-batch
            batch_root = td_p / "matches_batch"
            (batch_root / "8004002" / "inputs").mkdir(parents=True, exist_ok=True)
            payload_b2 = copy.deepcopy(payload)
            payload_b2["fixture"]["fixture_id"] += 1
            payload_b2["context"]["target_fixture_full"]["response"][0]["fixture"]["id"] = payload_b2["fixture"]["fixture_id"]
            payload_b2["match"]["event_id"] = "8004002"
            (batch_root / "8004002" / "inputs" / "match_engine_8004002.json").write_text(json.dumps(payload_b2), encoding="utf-8")

            p_batch = subprocess.run(
                [
                    sys.executable,
                    "tools/prospective_tracker.py",
                    "--track",
                    "2b3b",
                    "record-batch",
                    "--matches-root",
                    str(batch_root),
                    "--manifest",
                    str(CAND_MANIFEST_PATH),
                    "--records-dir",
                    str(rec_dir),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(p_batch.returncode, 0, p_batch.stderr + p_batch.stdout)
            batch_doc = json.loads(p_batch.stdout)
            self.assertEqual(batch_doc["recorded_count"], 1)

            # Verificar CLI legacy (--track legacy evaluate sobre dir vacío) sin regresiones
            legacy_dir = td_p / "legacy_rec"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            p_leg = subprocess.run(
                [
                    sys.executable,
                    "tools/prospective_tracker.py",
                    "--track",
                    "legacy",
                    "evaluate",
                    "--records-dir",
                    str(legacy_dir),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(p_leg.returncode, 0, p_leg.stderr + p_leg.stdout)
            leg_doc = json.loads(p_leg.stdout)
            self.assertEqual(leg_doc["settled_paired_n"], 0)
            self.assertTrue(rec_file.is_file())


if __name__ == "__main__":
    unittest.main()
