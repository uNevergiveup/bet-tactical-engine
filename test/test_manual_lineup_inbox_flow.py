"""Pruebas integrales offline del flujo futuro de XI manuales asistido por Codex:
1. API sin 22 titulares -> bandeja `outputs/matches/<EVENT_ID>/lineups/manual/` + estado LINEUPS_PENDING hasta corte.
2. Capturas colocadas en `lineups/manual/` preservadas por SHA-256 en espera de interpretación visual Codex.
3. Rechazo de solapamiento titulares/suplentes y no promoción de XI probables (LINEUPS_PROBABLE_ONLY).
4. Ingesta automática de JSON confirmado en `lineups/manual/` -> validación completa + recálculo Shadow.
5. Ausencia de capturas/JSON al corte -> descarte automático (`LINEUPS_UNAVAILABLE_FINAL`) y continuación al siguiente candidato elegible.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import prematch_case as pc
from bet_engine.context import LineupStatus, ObservationStore
from test_lineup_fallback_e2e import _clone_match_payload_with_kickoff, _rewrite_odds_txt
from test_lineup_management_p0 import _build_isolated_case_from_7912816, _make_manual_lineup_doc

ROOT = Path(__file__).resolve().parent.parent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ManualLineupInboxFlowTests(unittest.TestCase):
    def test_inbox_pending_screenshots_probable_confirmed_and_cutoff_fallback(self) -> None:
        me_path = ROOT / "outputs/matches/7912816/inputs/match_engine_7912816.json"
        me_b_src = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.json"
        me_b_txt = ROOT / "outputs/matches/7909704/inputs/match_engine_7909704.txt"
        odds_b_src = ROOT / "outputs/matches/7909704/inputs/cuotas_bwin_7909704.txt"
        if not (me_path.is_file() and me_b_src.is_file() and me_b_txt.is_file() and odds_b_src.is_file()):
            self.skipTest("Snapshots 7912816 / 7909704 no presentes")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            setup = _build_isolated_case_from_7912816(root)
            sel = setup["sel_doc"]
            fid = int(sel["fixture_id"])
            hid, aid = sel["home_api_id"], sel["away_api_id"]
            hname, aname = sel["home"], sel["away"]

            # 1) Antes del corte (T-40m > 15m): sin capturas ni JSON -> LINEUPS_PENDING + plantilla y prompt Codex creados
            res_empty = pc.sync_manual_lineups(
                root, "7912816", now_utc="2026-11-16T22:20:00+00:00", lineup_cutoff_minutes=15
            )
            self.assertEqual(res_empty["lineup_status"], LineupStatus.LINEUPS_PENDING.value)
            self.assertEqual(res_empty["inbox_state"], "MANUAL_INBOX_EMPTY")
            self.assertTrue(Path(res_empty["template_path"]).is_file())
            self.assertTrue(Path(res_empty["codex_prompt_path"]).is_file())

            # 2) Usuario coloca capturas simuladas en outputs/matches/7912816/lineups/manual/
            manual_dir = setup["mdir"] / "lineups" / "manual"
            img1 = manual_dir / "lineup_club_local.png"
            img2 = manual_dir / "lineup_sofascore_visitante.jpg"
            img1.write_bytes(b"\x89PNG\r\n\x1a\nsimulated-home-lineup-image")
            img2.write_bytes(b"\xff\xd8\xff\xe0simulated-away-lineup-image")
            sha_img1, sha_img2 = _sha(img1), _sha(img2)

            res_wait = pc.sync_manual_lineups(
                root, "7912816", now_utc="2026-11-16T22:25:00+00:00", lineup_cutoff_minutes=15
            )
            self.assertEqual(res_wait["lineup_status"], LineupStatus.LINEUPS_PENDING.value)
            self.assertEqual(res_wait["inbox_state"], "AWAITING_CODEX_VISUAL_INTERPRETATION")
            self.assertEqual(len(res_wait["screenshots_preserved"]), 2)
            self.assertEqual({s["sha256"] for s in res_wait["screenshots_preserved"]}, {sha_img1, sha_img2})

            # 3a) Si Codex interpreta la captura como PROBABLE -> LINEUPS_PROBABLE_ONLY y NO confirma
            prob_json = manual_dir / "manual_lineup_7912816.json"
            prob_doc = _make_manual_lineup_doc(
                fixture_id=fid,
                event_id="7912816",
                home_id=hid,
                home_name=hname,
                away_id=aid,
                away_name=aname,
                captured_at_utc="2026-11-16T22:24:00+00:00",
                source="Captura Diario Local (Posible XI)",
                source_url=None,
                source_kind="PROBABLE",
                confirmed=False,
            )
            prob_doc["screenshots"] = [{"path": img1.name, "sha256": sha_img1}]
            prob_json.write_text(json.dumps(prob_doc, indent=2), encoding="utf-8")

            res_prob = pc.sync_manual_lineups(
                root, "7912816", now_utc="2026-11-16T22:26:00+00:00", lineup_cutoff_minutes=15
            )
            self.assertEqual(res_prob["lineup_status"], LineupStatus.LINEUPS_PROBABLE_ONLY.value)
            self.assertFalse(res_prob["confirmed_complete_22"])
            self.assertEqual(res_prob["inbox_state"], "PROBABLE_ONLY_REJECTED_FOR_CONFIRMATION")

            # 3b) Solapamiento titular + suplente es rechazado cerrado (no confundir titulares con suplentes)
            bad_sub_doc = _make_manual_lineup_doc(
                fixture_id=fid,
                event_id="7912816",
                home_id=hid,
                home_name=hname,
                away_id=aid,
                away_name=aname,
                captured_at_utc="2026-11-16T22:30:00+00:00",
                source="Sofascore Confirmado",
                source_url=None,
                source_kind="EXTERNAL_PROVIDER",
                confirmed=True,
            )
            bad_sub_doc["teams"]["home"]["substitutes"] = [dict(bad_sub_doc["teams"]["home"]["starters"][0])]
            bad_json = root / "bad_overlap.json"
            bad_json.write_text(json.dumps(bad_sub_doc, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "LINEUP_STARTER_ALSO_IN_SUBSTITUTES"):
                pc.attach_manual_lineups(root, "7912816", bad_json, now_utc="2026-11-16T22:31:00+00:00")

            # 4) Captura oficial confirmada -> JSON Codex válido en lineups/manual/ -> sync CLI + recálculo Shadow
            conf_doc = _make_manual_lineup_doc(
                fixture_id=fid,
                event_id="7912816",
                home_id=hid,
                home_name=hname,
                away_id=aid,
                away_name=aname,
                captured_at_utc="2026-11-16T22:32:00+00:00",
                source="Comunicado Oficial Club + Sofascore",
                source_url=None,
                source_kind="OFFICIAL_CLUB",
                confirmed=True,
            )
            conf_doc["teams"]["home"]["substitutes"] = [{"name": "Bench Home 1"}, {"name": "Bench Home 2"}]
            conf_doc["screenshots"] = [
                {"path": img1.name, "sha256": sha_img1},
                {"path": img2.name, "sha256": sha_img2},
            ]
            conf_doc["uncertainties"] = []
            prob_json.write_text(json.dumps(conf_doc, indent=2), encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                rc = pc.main(
                    [
                        "--root",
                        str(root),
                        "sync-manual-lineups",
                        "--event-id",
                        "7912816",
                        "--now-utc",
                        "2026-11-16T22:35:00+00:00",
                    ]
                )
            self.assertEqual(rc, 0)

            sh_sum = json.loads((setup["mdir"] / "shadow/shadow_summary_7912816.json").read_text(encoding="utf-8"))
            self.assertEqual(sh_sum["lineup_gate"]["status"], LineupStatus.LINEUPS_MANUAL_VERIFIED.value)
            self.assertTrue(sh_sum["eligible_for_final_action"])
            self.assertEqual(_sha(img1), sha_img1)
            self.assertEqual(_sha(img2), sha_img2)
            self.assertEqual(_sha(setup["odds_dst"]), setup["original_shas"]["odds"])

        # 5) Caso sin capturas ni manual JSON al corte -> descarte automático y preparación del siguiente candidato B
        with tempfile.TemporaryDirectory() as td2:
            root2 = Path(td2)
            _build_isolated_case_from_7912816(root2)
            b_ko = "2026-11-17T00:20:00+00:00"
            b_src = root2 / "pool_b"
            me_b = _clone_match_payload_with_kickoff(
                me_b_src, b_src / "me_b.json", new_kickoff_utc=b_ko, new_event_id="7909704"
            )
            b_obs = ObservationStore.from_match_engine_json(me_b, cutoff_utc="2026-11-16T10:00:00+00:00")
            odds_b = b_src / "odds_b.txt"
            _rewrite_odds_txt(odds_b_src, odds_b, event_id="7909704", capture_iso="2026-11-16T22:35:00+00:00")
            ranking_p = root2 / "ranking.json"
            ranking_p.write_text(
                json.dumps(
                    {
                        "ranked_matches": [
                            {"fixture_id": 1649063, "bwin_event_id": "7912816", "kickoff_madrid": "2026-11-17T00:00:00+01:00", "eligible": True},
                            {"fixture_id": 888001, "bwin_event_id": "888001", "kickoff_madrid": "2026-11-16T23:05:00+00:00", "eligible": True},
                            {"fixture_id": b_obs.fixture_id, "bwin_event_id": "7909704", "home": b_obs.home_name, "away": b_obs.away_name, "kickoff_madrid": b_ko, "eligible": True},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            res_fb = pc.resolve_lineups_or_fallback(
                root2,
                "7912816",
                ranking_path=ranking_p,
                lineup_cutoff_minutes=15,
                min_lead_minutes=45,
                user_manual_absent=False,
                now_utc="2026-11-16T22:48:00+00:00",
                auto_prepare_next=True,
                next_context_json=b_src / "me_b.json",
                next_context_txt=me_b_txt,
                next_odds_txt=odds_b,
            )
            self.assertEqual(res_fb["lineup_status"], LineupStatus.LINEUPS_UNAVAILABLE_FINAL.value)
            self.assertTrue(res_fb["discarded"])
            self.assertEqual(res_fb["next_candidate_reconsideration"]["selected_event_id"], "7909704")
            self.assertTrue((root2 / "outputs/matches/7909704/case.json").is_file())


if __name__ == "__main__":
    unittest.main()
