"""Regresión Fase A (recuperación discovery/reuse/scanner/RESEARCH_PENDING) y Fase B (Quick Context MVP)."""
import csv
import json
import shutil
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prematch_case
import prematch_prospective_trial as trial
import prematch_runtime as runtime
from bet_engine.analytics import TacticalInvestigator, build_stage3_registry
from bet_engine.context import ObservationStore
from bet_engine.context.quick_context import parse_quick_context_markdown
from bet_engine.contracts.epistemic import EpistemicContractError, TemporalLeakageError
from bet_engine.modeling import CoherentMatchModeler

ROOT = Path(__file__).resolve().parent.parent
REAL_EVAL1 = ROOT / "outputs/discovery/2026-11-16_1500_0300/evaluations/20261116T121148466676Z_39af34fb2084"
REAL_EVAL2 = ROOT / "outputs/discovery/2026-11-16_1500_0300/evaluations/20261116T132006599723Z_39af34fb2084"


class TestFaseARecovery(unittest.TestCase):
    def test_classify_existing_snapshot_distinguishes_causes(self):
        now = datetime.fromisoformat("2026-11-16T14:21:30+01:00")
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            fresh = tdp / "cuotas_bwin_7909704.txt"
            fresh.write_text(
                "Event ID: 7909704\n"
                "URL: https://www.bwin.es/es/sports/eventos/estonia-bulgaria-2:7909704\n"
                "Captura: 2026-11-16T13:14:35+01:00\n"
                "MERCADO: Resultado del partido | Tiempo reglamentario\nEstonia : 1.90\n",
                encoding="utf-8",
            )
            cls, _ = runtime.classify_existing_snapshot(
                fresh, "7909704", "https://www.bwin.es/es/sports/eventos/estonia-bulgaria-2:7909704", now, 120
            )
            self.assertEqual(cls, "REUSABLE_EXISTING_SNAPSHOT")

            cls_stale, _ = runtime.classify_existing_snapshot(
                fresh, "7909704", "https://www.bwin.es/es/sports/eventos/estonia-bulgaria-2:7909704", now, 30
            )
            self.assertEqual(cls_stale, "STALE_EXISTING_ODDS")

            cls_wrong_id, _ = runtime.classify_existing_snapshot(
                fresh, "9999999", "https://www.bwin.es/es/sports/eventos/estonia-bulgaria-2:7909704", now, 120
            )
            self.assertEqual(cls_wrong_id, "INCOMPATIBLE_EXISTING_SNAPSHOT")

            cls_wrong_url, _ = runtime.classify_existing_snapshot(
                fresh, "7909704", "https://www.bwin.es/es/sports/eventos/otro-partido-2:7909704", now, 120
            )
            self.assertEqual(cls_wrong_url, "INCOMPATIBLE_EXISTING_SNAPSHOT")

    @unittest.skipUnless(REAL_EVAL1.is_dir() and REAL_EVAL2.is_dir(), "Evaluaciones reales 2026-11-16 no presentes")
    def test_real_failed_evaluation_offline_replay_activates_scanner_and_research_pending(self):
        at_1421 = datetime.fromisoformat("2026-11-16T14:21:30+01:00")
        with tempfile.TemporaryDirectory(dir=ROOT / "outputs/discovery") as td:
            win = Path(td) / "2026-11-16_1500_0300" / "evaluations"
            win.mkdir(parents=True)
            (win / REAL_EVAL1.name).symlink_to(REAL_EVAL1)
            eval2_tmp = win / REAL_EVAL2.name
            shutil.copytree(REAL_EVAL2, eval2_tmp)
            shutil.rmtree(eval2_tmp / "prospective_trial" / "daily_runs", ignore_errors=True)
            shutil.rmtree(eval2_tmp / "prospective_trial" / "preflight", ignore_errors=True)

            manifest_data = json.loads((eval2_tmp / "evaluation_manifest.json").read_text(encoding="utf-8"))
            args = SimpleNamespace(
                start=time(15, 0),
                end=time(3, 0),
                min_lead=45,
                horizon_hours=0,
                max_scrapes=4,
                max_odds_age=120,
                discovery_retries=1,
                history=20,
                details=8,
                evaluation_budget=0,
                discovery_budget=0,
                refresh_existing_odds=False,
                asof=at_1421.isoformat(),
                team=[],
            )
            real_run = trial.run

            def run_at_asof(r, script, sargs, logs):
                res = real_run(r, script, sargs, logs)
                if script == "bwin_market_preflight.py" and "--output-dir" in sargs:
                    p = Path(sargs[sargs.index("--output-dir") + 1]) / "market_preflight.json"
                    if p.is_file():
                        d = json.loads(p.read_text(encoding="utf-8"))
                        d["generated_at_utc"] = at_1421.astimezone().isoformat()
                        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                elif script == "prematch_opportunity_scanner.py" and "--output-dir" in sargs:
                    p = Path(sargs[sargs.index("--output-dir") + 1]) / "opportunity_scan.json"
                    if p.is_file():
                        d = json.loads(p.read_text(encoding="utf-8"))
                        d["asof_madrid"] = at_1421.isoformat()
                        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                return res

            with patch.object(trial, "now_madrid", return_value=at_1421), patch.object(
                trial, "evaluation_manifest", return_value=manifest_data
            ), patch.object(trial, "run", side_effect=run_at_asof):
                eval1_snaps = REAL_EVAL1 / "prospective_trial/daily_runs/20261116T131401071976/comparison_audit/snapshots"
                saved_current = {}
                try:
                    if eval1_snaps.is_dir():
                        for sp in eval1_snaps.glob("cuotas_bwin_*.txt"):
                            eid = sp.stem.split("_")[-1]
                            live_target = ROOT / "outputs/matches" / eid / "inputs" / sp.name
                            if live_target.is_file():
                                saved_current[live_target] = live_target.read_bytes()
                                live_target.write_bytes(sp.read_bytes())
                    doc = runtime.scan(ROOT, eval2_tmp, date(2026, 11, 16), args)
                finally:
                    for live_target, raw_bytes in saved_current.items():
                        live_target.write_bytes(raw_bytes)

            self.assertEqual(doc["status"], "READY_FOR_MANUAL_RESEARCH")
            self.assertIsNotNone(doc["scanner"])
            self.assertTrue(Path(doc["scanner"]).is_file())
            self.assertGreater(doc["phase_metrics"]["commercial_scanner"], 0.0)
            self.assertEqual(doc["comparison_verified_events"], 4)
            pending_fids = {r["fixture_id"]: r for r in doc["research_pending_candidates"]}
            self.assertIn(1528986, pending_fids)
            self.assertFalse(pending_fids[1528986]["commercial_verified"])
            self.assertFalse(pending_fids[1528986]["offer_proven_absent"])

            with (eval2_tmp / "prospective_trial/partidos_diarios.csv").open(encoding="utf-8-sig", newline="") as fh:
                csv_rows = {int(r["fixture_id"]): r for r in csv.DictReader(fh, delimiter=";")}
            self.assertEqual(csv_rows[1528986]["estado_final"], "RESEARCH_PENDING")
            self.assertIn("oferta Bwin no descartada", csv_rows[1528986]["motivo_exclusion_o_pendiente"])


class TestFaseBQuickContext(unittest.TestCase):
    SAMPLE_KAZ_SVK_MD = """# Quick Context PREMATCH: Kazakhstan vs Slovakia

- fixture_id: 1528986
- bwin_event_id: PENDIENTE_VERIFICACION
- partido: Kazakhstan vs Slovakia
- competition: UEFA Nations League (League C - 2026)
- kickoff_madrid: 2026-11-16T16:00:00+01:00
- kickoff_utc: 2026-11-16T15:00:00+00:00
- research_timestamp_utc: 2026-11-16T13:30:00+00:00
- analysis_cutoff_utc: 2026-11-16T13:40:00+00:00

## Hechos confirmados
- [CONFIRMADO] Eslovaquia ya tiene asegurado el ascenso/liderato de grupo antes de la jornada final; fuente federativa https://futbalsfz.sk/reprezentacia-a-liga-narodov-2026 (2026-11-15).
- [CONFIRMADO] El encuentro se disputa en el Astana Arena sobre césped artificial bajo techo; ficha UEFA https://www.uefa.com/uefanationsleague/match/1528986 (2026-11-16).
- [CONFIRMADO] Convocatoria oficial de Eslovaquia difundida prepartido con bajas por descanso/no viaje de tres habituales respecto a la ventana previa https://futbalsfz.sk/nominacia-astana-2026 (2026-11-15).

## Hechos probables
- [PROBABLE] Rotación parcial en Eslovaquia dando minutos a jugadores con menor carga competitiva tras el desplazamiento largo a Kazajistán; prensa deportiva https://sport.aktuality.sk/clanok/slovensko-kazachstan-zostava (2026-11-16).

## Inferencias tácticas (no son probabilidades ni pronósticos)
- [INFERIDO] La superficie sintética del Astana Arena junto al posible XI alternativo visitante puede alterar ritmo de apoyos, tiros bloqueados y córners respecto al histórico en césped natural.

## No verificados e hipótesis externas
- [HIPOTESIS_TIPSTER] Un tipster afirma en redes que "descansan doce titulares" de Eslovaquia; no existe XI confirmado oficial todavía (2026-11-16). https://example.com/tipster-rumor

## Contradicciones
- [CONTRADICCION] Convocatoria oficial y prensa local mencionan rotación parcial; la afirmación de "doce titulares descansando" carece de confirmación oficial y no equivale a XI confirmado.
"""

    def test_kazakhstan_slovakia_1528986_quick_context_import_dedup_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pre_dir = root / "outputs/discovery/2026-11-16_1500_0300/evaluations/test_eval/prospective_trial/enriched"
            pre_dir.mkdir(parents=True)
            pre_path = pre_dir / "preselection.json"
            pre_path.write_text(
                json.dumps(
                    {
                        "matches": [
                            {
                                "fixture_id": 1528986,
                                "home": "Kazakhstan",
                                "away": "Slovakia",
                                "league": "UEFA Nations League",
                                "season": 2026,
                                "kickoff_madrid": "2026-11-16T16:00:00+01:00",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            md_path = root / "qc_kaz_svk.md"
            md_path.write_text(self.SAMPLE_KAZ_SVK_MD, encoding="utf-8")

            with patch("bet_engine.context.quick_context.datetime") as mock_dt:
                # Simular instante válido antes del kickoff (14:45 CET = 13:45 UTC)
                now_val = datetime.fromisoformat("2026-11-16T13:45:00+00:00")
                mock_dt.now.return_value = now_val
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                res1 = prematch_case.import_quick_context(root, "1528986", md_path, pre_path)
                res2 = prematch_case.import_quick_context(root, "1528986", md_path, pre_path)

            self.assertEqual(res1["status"], "QUICK_CONTEXT_IMPORTED")
            self.assertFalse(res1["duplicate"])
            self.assertFalse(res1["p_model_modified"])
            self.assertEqual(res1["target_kind"], "RESEARCH_PENDING_FIXTURE")
            self.assertIsNone(res1["bwin_event_id"])
            self.assertGreaterEqual(res1["evidence_count"], 5)
            areas = {h["hypothesis_area"] for h in res1["affected_hypotheses"]}
            self.assertIn("SURFACE_ARTIFICIAL_TURF", areas)
            self.assertIn("LINEUP_ROTATION_EXPOSURE", areas)
            self.assertIn("COMPETITIVE_INCENTIVE_SHIFT", areas)
            self.assertTrue(any("doce titulares" in c or "rotación" in c.lower() for c in res1["contradictions"]))
            self.assertEqual(Path(res1["preserved_md"]).read_bytes(), md_path.read_bytes())

            self.assertTrue(res2["duplicate"])
            self.assertEqual(res2["status"], "ALREADY_IMPORTED_DUPLICATE_PREVENTED")

    def test_quick_context_rejects_post_kickoff_and_identity_mismatch(self):
        with self.assertRaises(TemporalLeakageError):
            parse_quick_context_markdown(
                self.SAMPLE_KAZ_SVK_MD.replace("analysis_cutoff_utc: 2026-11-16T13:40:00+00:00", "analysis_cutoff_utc: 2026-11-16T15:05:00+00:00"),
                expected_fixture_id=1528986,
                expected_event_id=None,
                expected_home="Kazakhstan",
                expected_away="Slovakia",
                expected_kickoff_utc="2026-11-16T15:00:00+00:00",
                now_utc=datetime.fromisoformat("2026-11-16T15:10:00+00:00"),
            )
        with self.assertRaises(EpistemicContractError):
            parse_quick_context_markdown(
                self.SAMPLE_KAZ_SVK_MD,
                expected_fixture_id=9999999,
                expected_event_id=None,
                expected_home="Kazakhstan",
                expected_away="Slovakia",
                expected_kickoff_utc="2026-11-16T15:00:00+00:00",
                now_utc=datetime.fromisoformat("2026-11-16T13:45:00+00:00"),
            )

    def test_quick_context_does_not_alter_p_model_and_integrates_in_dossier(self):
        real_json = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.json"
        if not real_json.is_file():
            self.skipTest("match_engine_7909704.json no disponible")
        payload = json.loads(real_json.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            case_dir = Path(td) / "7909704"
            bundle_without = ObservationStore.from_match_engine_json(
                payload, cutoff_utc="2026-11-16T13:40:00+00:00", case_dir=case_dir
            )
            model_without = CoherentMatchModeler.build_model(bundle_without)

            md_7909704 = case_dir / "qc_7909704.md"
            case_dir.mkdir(parents=True, exist_ok=True)
            md_7909704.write_text(
                "# Quick Context: Estonia vs Bulgaria\n"
                "- fixture_id: 1528988\n"
                "- bwin_event_id: 7909704\n"
                "- home: Estonia\n- away: Bulgaria\n"
                "- competition: UEFA Nations League\n"
                "- kickoff_utc: 2026-11-16T17:00:00+00:00\n"
                "- research_timestamp_utc: 2026-11-16T13:30:00+00:00\n"
                "- analysis_cutoff_utc: 2026-11-16T13:35:00+00:00\n\n"
                "## Hechos confirmados\n"
                "- [CONFIRMADO] Ambos combinados disputaron su último duelo hace ~69h https://example.com/uefa (2026-11-16).\n",
                encoding="utf-8",
            )
            from bet_engine.context.quick_context import import_quick_context_markdown

            import_quick_context_markdown(
                case_dir,
                md_7909704,
                expected_fixture_id=1528988,
                expected_event_id="7909704",
                expected_home="Estonia",
                expected_away="Bulgaria",
                expected_kickoff_utc="2026-11-16T17:00:00+00:00",
                now_utc=datetime.fromisoformat("2026-11-16T13:45:00+00:00"),
            )
            bundle_with = ObservationStore.from_match_engine_json(
                payload, cutoff_utc="2026-11-16T13:40:00+00:00", case_dir=case_dir
            )
            res = build_stage3_registry().run_all(bundle_with)
            inv = TacticalInvestigator()
            dossier = inv.build_dossier(bundle_with, res)
            prompt_md = inv.render_markdown_prompt(dossier)
            model_with = CoherentMatchModeler.build_model(bundle_with)

            self.assertIn("Quick Context Cualitativo Importado", prompt_md)
            self.assertEqual(model_without.to_dict(), model_with.to_dict())


if __name__ == "__main__":
    unittest.main()
