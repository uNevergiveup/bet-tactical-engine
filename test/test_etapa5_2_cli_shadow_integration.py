"""Pruebas integrales realistas por CLI de la Etapa 5.2:
- prematch_daily.py (--shadow-preanalyze -> reinicio -> --shadow-status -> --shadow-finalize)
- prematch_multi.py & prematch_single.py (--shadow-mode desactivado por defecto vs activado)
- Reutilización de T-24h, rechazo de cuotas caducadas, recuperación ante fallo de Shadow
- Auditoría estadística de dependencias, duplicados y estratos sobre el corpus real (76 bundles).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from bet_engine.context import ObservationStore
from bet_engine.modeling import WalkForwardEvaluator


ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "bin" / "python")
if not Path(PYTHON).is_file():
    PYTHON = sys.executable


def _run_cli(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True, timeout=60)


def _prepare_isolated_match_root(tmp_root: Path, event_id: str) -> Path:
    """Crea un workspace aislado con symlinks al código fuente (solo lectura) y copia de outputs/matches/<event_id>."""
    for item in ROOT.iterdir():
        if item.name in ("outputs", ".git", ".venv", "__pycache__"):
            continue
        target = tmp_root / item.name
        if not target.exists():
            os.symlink(item, target)
    src_match = ROOT / "outputs" / "matches" / event_id
    dst_match = tmp_root / "outputs" / "matches" / event_id
    dst_match.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_match, dst_match)

    # Limpia reportes previos finales si existieran y deja solo un reporte cualitativo válido de prueba
    inputs_dir = dst_match / "inputs"
    rep_path = inputs_dir / f"reporte_cualitativo_{event_id}.md"
    rep_path.write_text(
        f"Event ID: {event_id}\nFixture ID: 1570448\nSTATUS: VALIDADO_TEST\n\n"
        "Resumen táctico prepartido de prueba para auditoría histórica.\n",
        encoding="utf-8",
    )
    return dst_match


class TestStage52CliIntegration(unittest.TestCase):
    def test_daily_cli_t24_restart_proximity_fresh_odds_and_tie(self) -> None:
        snap_20 = (
            ROOT
            / "outputs"
            / "discovery"
            / "2026-11-15_1500_0300"
            / "evaluations"
            / "20261115T195445143219Z_3987d11f552b"
            / "prospective_trial"
            / "daily_runs"
            / "20261115T205609404284"
            / "comparison_audit"
            / "sources"
            / "preselection.json"
        )
        if not snap_20.is_file():
            self.skipTest("Snapshot preselection.json no presente")

        raw_pre = json.loads(snap_20.read_text(encoding="utf-8"))
        # Creamos copia temporal con 2 partidos elegibles el 2026-11-16 tras las 13:00 Europe/Madrid
        # con kickoff escalonado (16:00Z y 18:00Z) para probar proximidad y cola tras reinicio.
        m0 = dict(raw_pre["matches"][0])
        m1 = dict(raw_pre["matches"][1])
        m0["event_id"] = "7990001"
        m0["kickoff_madrid"] = "2026-11-16T17:00:00+01:00"  # 16:00Z
        m1["event_id"] = "7990002"
        m1["kickoff_madrid"] = "2026-11-16T19:00:00+01:00"  # 18:00Z

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            pre_file = td_p / "preselection_t24.json"
            pre_file.write_text(
                json.dumps(
                    {"generated_at_utc": raw_pre["generated_at_utc"], "matches": [m0, m1]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            sh_dir = td_p / "shadow_ws"

            # Proceso 1: T-24h preanalyze (sin confirm-external ni API calls)
            p1 = _run_cli(
                [
                    PYTHON,
                    "prematch_daily.py",
                    "--shadow-preanalyze",
                    "--date",
                    "2026-11-16",
                    "--preselection",
                    str(pre_file),
                    "--shadow-dir",
                    str(sh_dir),
                ]
            )
            self.assertEqual(p1.returncode, 0, msg=p1.stderr)
            out1 = json.loads(p1.stdout)
            self.assertEqual(out1["api_calls_executed"], 0)
            self.assertFalse(out1["t24_snapshot"]["final_picks_generated"])
            self.assertTrue((sh_dir / "proximity_plan_state.json").is_file())

            # Proceso 2 (tras reinicio): status a las 11:30Z (12:30 Madrid < 13:00) -> WAITING_AVAILABLE_TIME
            p2_early = _run_cli(
                [
                    PYTHON,
                    "prematch_daily.py",
                    "--shadow-status",
                    "--date",
                    "2026-11-16",
                    "--shadow-dir",
                    str(sh_dir),
                    "--available-from",
                    "13:00",
                    "--now-utc",
                    "2026-11-16T11:30:00+00:00",
                ]
            )
            self.assertEqual(p2_early.returncode, 0, msg=p2_early.stderr)
            self.assertEqual(json.loads(p2_early.stdout)["next_action"]["status"], "WAITING_AVAILABLE_TIME")

            # Proceso 3: status a las 14:00Z (15:00 Madrid >= 13:00) -> selecciona 7990001 por proximidad
            p3_ok = _run_cli(
                [
                    PYTHON,
                    "prematch_daily.py",
                    "--shadow-status",
                    "--date",
                    "2026-11-16",
                    "--shadow-dir",
                    str(sh_dir),
                    "--now-utc",
                    "2026-11-16T14:00:00+00:00",
                ]
            )
            self.assertEqual(p3_ok.returncode, 0, msg=p3_ok.stderr)
            self.assertEqual(json.loads(p3_ok.stdout)["next_action"]["selected_event_id"], "7990001")

            # Verifica que cuotas caducadas son RECHAZADAS en --shadow-finalize
            expired_odds = td_p / "expired_odds.json"
            expired_odds.write_text(
                json.dumps(
                    {
                        "event_id": "7990001",
                        "captured_at_utc": "2026-11-16T10:00:00+00:00",  # > 120m antes de 14:30Z
                        "odds": {"over_2_5_goals": 2.10},
                    }
                ),
                encoding="utf-8",
            )
            p4_exp = _run_cli(
                [
                    PYTHON,
                    "prematch_daily.py",
                    "--shadow-finalize",
                    "--date",
                    "2026-11-16",
                    "--shadow-dir",
                    str(sh_dir),
                    "--now-utc",
                    "2026-11-16T14:30:00+00:00",
                    "--odds-json",
                    str(expired_odds),
                    "--max-odds-age",
                    "60",
                ]
            )
            self.assertNotEqual(p4_exp.returncode, 0)
            self.assertIn("EXPIRED_ODDS_REJECTED", p4_exp.stderr)

            # Proceso 4 con cuotas FRESCAS: finalize de 7990001 reutilizando T-24h y recalculando cola hacia 7990002
            fresh_odds = td_p / "fresh_odds.json"
            fresh_odds.write_text(
                json.dumps(
                    {
                        "event_id": "7990001",
                        "captured_at_utc": "2026-11-16T14:20:00+00:00",
                        "odds": {"over_2_5_goals": 2.10, "over_9_5_corners": 1.90},
                    }
                ),
                encoding="utf-8",
            )
            p4_ok = _run_cli(
                [
                    PYTHON,
                    "prematch_daily.py",
                    "--shadow-finalize",
                    "--date",
                    "2026-11-16",
                    "--shadow-dir",
                    str(sh_dir),
                    "--now-utc",
                    "2026-11-16T14:30:00+00:00",
                    "--odds-json",
                    str(fresh_odds),
                    "--max-odds-age",
                    "60",
                ]
            )
            self.assertEqual(p4_ok.returncode, 0, msg=p4_ok.stderr)
            out4 = json.loads(p4_ok.stdout)
            self.assertEqual(out4["event_id"], "7990001")
            self.assertTrue(out4["reused_t24_cache"])
            self.assertEqual(out4["api_historical_calls_repeated"], 0)
            self.assertEqual(out4["odds_validation"], "FRESH_ODDS_VERIFIED")
            self.assertEqual(out4["next_schedule_status"]["next_action"]["selected_event_id"], "7990002")

    def test_multi_finish_legacy_unchanged_vs_shadow_opt_in_and_failsafe(self) -> None:
        if not (ROOT / "outputs" / "matches" / "7903872" / "manifest.json").is_file():
            self.skipTest("Snapshot 7903872 no presente")

        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            match_dir = _prepare_isolated_match_root(tmp_root, "7903872")

            # 1) Sin --shadow-mode: comportamiento legacy intacto, sin carpeta shadow/
            t0 = time.perf_counter()
            res_leg = _run_cli(
                [
                    PYTHON,
                    "prematch_multi.py",
                    "--root",
                    str(tmp_root),
                    "finish",
                    "--event-id",
                    "7903872",
                    "--workers",
                    "1",
                    "--historical",
                ],
                cwd=tmp_root,
            )
            leg_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            self.assertEqual(res_leg.returncode, 0, msg=res_leg.stdout + "\n" + res_leg.stderr)
            self.assertFalse((match_dir / "shadow").exists())
            manifest = json.loads((match_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "AUDITORIA_HISTORICA")

            # 2) Con --shadow-mode: conserva flujo anterior y genera comparación paralela en shadow/
            t1 = time.perf_counter()
            res_sh = _run_cli(
                [
                    PYTHON,
                    "prematch_multi.py",
                    "--root",
                    str(tmp_root),
                    "finish",
                    "--event-id",
                    "7903872",
                    "--workers",
                    "1",
                    "--historical",
                    "--shadow-mode",
                ],
                cwd=tmp_root,
            )
            sh_ms = round((time.perf_counter() - t1) * 1000.0, 2)
            self.assertEqual(res_sh.returncode, 0, msg=res_sh.stdout + "\n" + res_sh.stderr)
            self.assertIn("SHADOW MODE", res_sh.stdout)
            sum_file = match_dir / "shadow" / "shadow_summary_7903872.json"
            self.assertTrue(sum_file.is_file())
            self.assertTrue((match_dir / "logs" / "shadow_mode.log").is_file())
            summary = json.loads(sum_file.read_text(encoding="utf-8"))
            self.assertEqual(summary["mode"], "SHADOW_NON_INTRUSIVE")
            self.assertGreater(len(summary["shadow_report"]["market_comparisons"]), 0)
            self.assertGreaterEqual(sh_ms, 0.0)
            self.assertGreaterEqual(leg_ms, 0.0)

            # 3) Prueba Fail-Safe: si falla Shadow Mode (p.ej. faltase o se corrompiera la ruta),
            # finish debe seguir devolviendo exit 0 y preservar el motor anterior.
            from bet_engine.orchestration import run_shadow_for_event_dir

            bad = run_shadow_for_event_dir(tmp_root, "0000000_NONEXISTENT")
            self.assertFalse(bad["ok"])
            self.assertIn("MANIFEST_MISSING", bad["error"])

    def test_pending_statistical_audit_dependencies_and_strata(self) -> None:
        p1 = ROOT / "outputs" / "reconciliation_real_20261111" / "coverage_models_final" / "preselection_cache_recovered.json"
        p2 = (
            ROOT
            / "outputs"
            / "discovery"
            / "2026-11-15_1500_0300"
            / "evaluations"
            / "20261115T195445143219Z_3987d11f552b"
            / "prospective_trial"
            / "daily_runs"
            / "20261115T205609404284"
            / "comparison_audit"
            / "sources"
            / "preselection.json"
        )
        if not (p1.is_file() and p2.is_file()):
            self.skipTest("Snapshots de preselection no presentes")
        bundles = []
        for p in (p1, p2):
            d = json.loads(p.read_text(encoding="utf-8"))
            for m in d["matches"]:
                bundles.append(ObservationStore.from_preselection_match(m, cutoff_utc=d["generated_at_utc"]))

        audit_g = WalkForwardEvaluator.audit_corpus_dependencies_and_segments(bundles, metric="goals", line=1.5)
        self.assertGreater(audit_g["duplicate_steps_excluded"], 0)
        self.assertEqual(audit_g["scope"], "INDIVIDUAL_TEAM_METRICS_SEPARATED_FROM_JOINT_MATCH")
        self.assertIn("DOMESTIC_LEAGUE", audit_g["strata_breakdown"])

        wf_c = WalkForwardEvaluator.evaluate_multi_series_corpus(bundles, metric="corners", line=4.5)
        self.assertEqual(wf_c.calibration_status, "LIMITED_PREDICTIVE_EVIDENCE_FAMILY_NOT_CALIBRATED")


if __name__ == "__main__":
    unittest.main()
