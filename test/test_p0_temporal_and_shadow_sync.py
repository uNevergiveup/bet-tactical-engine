#!/usr/bin/env python3
"""Mandatory P0 tests for Bwin temporal identity (P0-A) and Shadow synchronization (P0-B)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bwin_market_preflight as preflight
import generate_context as gc
import prematch_case
import prematch_single
import validate_prematch as vp
from bet_engine.orchestration.adapters import run_shadow_for_event_dir


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_odds_txt(
    event_id: str,
    home: str,
    away: str,
    captured_madrid: str,
    *,
    home_odd: float,
    draw_odd: float,
    away_odd: float,
    extra_selections: int = 0,
) -> str:
    slug = f"{home.lower().replace(' ', '-')}-{away.lower().replace(' ', '-')}"
    lines = [
        f"URL: https://www.bwin.es/es/sports/eventos/{slug}-2:{event_id}",
        f"Event ID: {event_id}",
        f"Partido: {home} vs {away}",
        f"Captura: {captured_madrid}",
        f"Acordeones: 2 | Selecciones: {3 + extra_selections}",
        "",
        "MERCADO: Resultado del partido | Tiempo reglamentario",
        f"{home} : {home_odd}",
        f"X : {draw_odd}",
        f"{away} : {away_odd}",
    ]
    if extra_selections > 0:
        lines.append("")
        lines.append("MERCADO: Total de goles | Tiempo reglamentario")
        for i in range(extra_selections):
            line_val = f"{0.5 + i:.1f}".replace(".", ",")
            lines.append(f"Más de {line_val} : {1.10 + (i % 10) * 0.05:.2f}")
    return "\n".join(lines) + "\n"


def _make_match_engine_json(event_id: str, fixture_id: int, home_id: int, home_name: str, away_id: int, away_name: str, kickoff_utc: str, generated_at: str) -> dict:
    def _hist_row(fid: int, date_iso: str, hid: int, hname: str, aid: int, aname: str, hg: int, ag: int) -> dict:
        return {
            "fixture": {"id": fid, "date": date_iso, "status": {"short": "FT"}},
            "league": {"id": 10, "name": "Friendlies", "season": 2026},
            "teams": {"home": {"id": hid, "name": hname}, "away": {"id": aid, "name": aname}},
            "goals": {"home": hg, "away": ag},
            "score": {"halftime": {"home": 0, "away": 0}},
            "statistics": [],
            "events": [],
            "lineups": [],
        }

    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "match": {
            "event_id": event_id,
            "url": f"https://www.bwin.es/es/sports/eventos/{home_name.lower()}-{away_name.lower().replace(' ', '-')}-2:{event_id}",
            "home_name": home_name,
            "away_name": away_name,
            "home_name_raw": home_name,
            "away_name_raw": away_name,
            "identity_status": "FIXTURE_AND_BWIN_TIME_VERIFIED",
        },
        "fixture": {
            "fixture_id": fixture_id,
            "date": kickoff_utc,
            "status": "NS",
            "status_long": "Not Started",
            "league_id": 10,
            "league_name": "Friendlies",
            "season": 2026,
            "home_id": home_id,
            "home_name": home_name,
            "away_id": away_id,
            "away_name": away_name,
        },
        "context": {
            "fixture": {
                "response": [
                    {
                        "fixture": {"id": fixture_id, "date": kickoff_utc, "status": {"short": "NS"}},
                        "league": {"id": 10, "name": "Friendlies", "season": 2026},
                        "teams": {"home": {"id": home_id, "name": home_name}, "away": {"id": away_id, "name": away_name}},
                    }
                ]
            },
            "home_history_deep": {
                "detailed_fixtures": [
                    _hist_row(9001 + i, "2026-10-01T20:00:00+00:00", home_id, home_name, 999, "Rival", 2, 0)
                    for i in range(4)
                ],
            },
            "away_history_deep": {
                "detailed_fixtures": [
                    _hist_row(9101 + i, "2026-10-01T20:00:00+00:00", 888, "Otro", away_id, away_name, 1, 1)
                    for i in range(4)
                ],
            },
            "lineups": {"response": []},
        },
    }


class TestP0ATemporalIdentity(unittest.TestCase):
    def test_valid_absolute_bwin_time_chile_colombia_pattern(self) -> None:
        catalog = [
            {
                "bwin_event_id": "7909714",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/chile-colombia-2:7909714",
                "text": "Chile Colombia Mañana / 0:00 1 3.70 X 3.25 2 2.00",
                "text_source": "SINGLE_EVENT_TILE",
                "text_observed_at_utc": "2026-11-17T14:50:04.312402+00:00",
                "browser_timezone": "Europe/Madrid",
            }
        ]
        info = gc.resolve_browser_event_kickoff(catalog, "7909714")
        self.assertIsNotNone(info)
        self.assertEqual(info["method"], "BWIN_VISIBLE_HOY_MANANA_TIME")
        self.assertEqual(info["kickoff_utc"].isoformat(), "2026-11-17T23:00:00+00:00")

    def test_valid_countdown_bwin_time_argentina_costa_rica_pattern(self) -> None:
        catalog = [
            {
                "bwin_event_id": "7909713",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                "text": "Argentina Costa Rica Comienza en 56 min CA 1 1.07 X 13.50 2 26.00",
                "text_source": "SINGLE_EVENT_TILE",
                "text_observed_at_utc": "2026-11-17T22:03:41.738633+00:00",
                "browser_timezone": "Europe/Madrid",
            }
        ]
        info = gc.resolve_browser_event_kickoff(catalog, "7909713")
        self.assertIsNotNone(info)
        self.assertEqual(info["method"], "BWIN_EVENT_COUNTDOWN_MINUTES")
        self.assertEqual(info["countdown_minutes"], 56)
        api_ko = datetime.fromisoformat("2026-11-17T23:00:00+00:00")
        diff_min = abs((api_ko - info["kickoff_utc"]).total_seconds()) / 60.0
        self.assertLessEqual(diff_min, 1.0)

        ev = preflight.time_evidence(
            {
                "bwin_event_id": "7909713",
                "kickoff_madrid": "2026-11-18T00:00:00+01:00",
            },
            catalog,
        )
        self.assertEqual(ev["status"], "COUNTDOWN_TIME_CORROBORATED")
        self.assertLessEqual(ev["difference_minutes"], 4.0)

    def test_missing_or_ambiguous_countdown_rejected(self) -> None:
        # Missing timestamp
        self.assertIsNone(
            gc.resolve_browser_event_kickoff(
                [
                    {
                        "bwin_event_id": "7909713",
                        "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                        "text": "Argentina Costa Rica Comienza en 56 min",
                        "text_source": "SINGLE_EVENT_TILE",
                        "browser_timezone": "Europe/Madrid",
                    }
                ],
                "7909713",
            )
        )
        # Ambiguous mixed patterns in one tile
        self.assertIsNone(
            gc.resolve_browser_event_kickoff(
                [
                    {
                        "bwin_event_id": "7909713",
                        "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                        "text": "Argentina Costa Rica Mañana / 0:00 Comienza en 56 min",
                        "text_source": "SINGLE_EVENT_TILE",
                        "text_observed_at_utc": "2026-11-17T22:03:41+00:00",
                        "browser_timezone": "Europe/Madrid",
                    }
                ],
                "7909713",
            )
        )
        # 0 min or >180 min rejected
        self.assertIsNone(
            gc.resolve_browser_event_kickoff(
                [
                    {
                        "bwin_event_id": "7909713",
                        "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                        "text": "Argentina Costa Rica Comienza en 0 min",
                        "text_source": "SINGLE_EVENT_TILE",
                        "text_observed_at_utc": "2026-11-17T22:03:41+00:00",
                        "browser_timezone": "Europe/Madrid",
                    }
                ],
                "7909713",
            )
        )

    def test_incompatible_event_id_rejected(self) -> None:
        catalog = [
            {
                "bwin_event_id": "7909714",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/chile-colombia-2:7909714",
                "text": "Chile Colombia Comienza en 56 min",
                "text_source": "SINGLE_EVENT_TILE",
                "text_observed_at_utc": "2026-11-17T22:03:41+00:00",
                "browser_timezone": "Europe/Madrid",
            }
        ]
        self.assertIsNone(gc.resolve_browser_event_kickoff(catalog, "7909713"))

    def test_excessive_time_difference_blocks(self) -> None:
        catalog = [
            {
                "bwin_event_id": "7909713",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                "text": "Argentina Costa Rica Comienza en 56 min",
                "text_source": "SINGLE_EVENT_TILE",
                "text_observed_at_utc": "2026-11-17T22:03:41+00:00",
                "browser_timezone": "Europe/Madrid",
            }
        ]
        ev = preflight.time_evidence(
            {
                "bwin_event_id": "7909713",
                "kickoff_madrid": "2026-11-18T00:30:00+01:00",  # 30 min off
            },
            catalog,
        )
        self.assertEqual(ev["status"], "DISPLAY_TIME_CONFLICT")
        self.assertGreater(ev["difference_minutes"], 4.0)

        # Also verify validate_prematch blocks when countdown evidence conflicts
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ident_path = root / "match_identity.json"
            api_txt = root / "match_engine_7909713.txt"
            api_json = root / "match_engine_7909713.json"
            ident_doc = {
                "schema_version": "1.0-fixture-bound",
                "identity_status": "FIXTURE_AND_BWIN_TIME_VERIFIED",
                "bwin_event_id": "7909713",
                "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                "api_fixture_id": 1647372,
                "league_id": 10,
                "season": 2026,
                "kickoff_utc": "2026-11-17T23:30:00+00:00",
                "home": {"api_team_id": 26, "api_name": "Argentina", "bwin_name": "Argentina", "category": "male"},
                "away": {"api_team_id": 29, "api_name": "Costa Rica", "bwin_name": "Costa Rica", "category": "male"},
                "temporal_identity_evidence": {
                    "schema_version": "1.1-browser-event-time",
                    "source": "SINGLE_EVENT_TILE",
                    "method": "BWIN_EVENT_COUNTDOWN_MINUTES",
                    "bwin_event_id": "7909713",
                    "observed_at_utc": "2026-11-17T22:03:41+00:00",
                    "countdown_minutes": 56,
                    "kickoff_utc": "2026-11-17T22:59:41+00:00",
                    "tolerance_minutes": 4,
                    "catalog_observations": [
                        {
                            "bwin_event_id": "7909713",
                            "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                            "text": "Argentina Costa Rica Comienza en 56 min",
                            "text_source": "SINGLE_EVENT_TILE",
                            "text_observed_at_utc": "2026-11-17T22:03:41+00:00",
                            "browser_timezone": "Europe/Madrid",
                        }
                    ],
                },
            }
            ident_path.write_text(json.dumps(ident_doc), encoding="utf-8")
            api_json_doc = _make_match_engine_json(
                "7909713", 1647372, 26, "Argentina", 29, "Costa Rica",
                "2026-11-17T23:30:00+00:00", "2026-11-17T22:03:45+00:00"
            )
            api_json_doc["match"]["temporal_identity_evidence"] = ident_doc["temporal_identity_evidence"]
            api_json.write_text(json.dumps(api_json_doc), encoding="utf-8")
            api_txt.write_text(
                "\n".join(
                    [
                        "Generado: 2026-11-17T22:03:45+00:00",
                        "Bwin URL: https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                        "Event ID: 7909713",
                        "Home Bwin: Argentina",
                        "Away Bwin: Costa Rica",
                        "Fixture ID: 1647372",
                        "Status: NS",
                        "Competition: Friendlies",
                        "Season: 2026",
                        "Home API: Argentina (ID 26)",
                        "Away API: Costa Rica (ID 29)",
                        "Identity Status: FIXTURE_AND_BWIN_TIME_VERIFIED",
                        "Date UTC: 2026-11-17T23:30:00+00:00",
                    ]
                ),
                encoding="utf-8",
            )
            out_dir = root / "val_out"
            out_dir.mkdir(parents=True, exist_ok=True)
            argv = [
                "validate_prematch.py",
                "--root", str(root),
                "--url", "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                "--api", str(api_txt),
                "--no-odds",
                "--historical",
                "--identity", str(ident_path),
                "--out", str(out_dir),
            ]
            with mock.patch.object(sys, "argv", argv):
                rc = vp.main()
            self.assertNotEqual(rc, 0)
            res = json.loads((out_dir / "validacion_7909713.json").read_text(encoding="utf-8"))
            codes = {i["code"] for i in res["issues"] if i["severity"] == "BLOCK"}
            self.assertIn("IDENTITY_INVALID", codes)

    def test_day_rollover_cet_cest_dst_and_tampered_countdown_rejected(self) -> None:
        # Midnight day rollover in Europe/Madrid (23:50+01:00 + 20 min -> 00:10+01:00)
        rollover = gc.resolve_browser_event_kickoff(
            [
                {
                    "bwin_event_id": "7909713",
                    "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                    "text": "Argentina Costa Rica Comienza en 20 min",
                    "text_source": "SINGLE_EVENT_TILE",
                    "text_observed_at_utc": "2026-11-17T22:50:00+00:00",  # 23:50 CET
                    "browser_timezone": "Europe/Madrid",
                }
            ],
            "7909713",
        )
        self.assertIsNotNone(rollover)
        self.assertEqual(rollover["kickoff_utc"].isoformat(), "2026-11-17T23:10:00+00:00")

        # Ambiguous DST fold clock ("Hoy / 02:30" on 2026-10-25 in Europe/Madrid) must be rejected
        ambiguous_dst = gc.resolve_browser_event_kickoff(
            [
                {
                    "bwin_event_id": "7909713",
                    "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                    "text": "Argentina Costa Rica Hoy / 02:30",
                    "text_source": "SINGLE_EVENT_TILE",
                    "text_observed_at_utc": "2026-10-24T23:30:00+00:00",
                    "browser_timezone": "Europe/Madrid",
                }
            ],
            "7909713",
        )
        self.assertIsNone(ambiguous_dst)

        # Countdown across DST fall-back in UTC stays unambiguous
        dst_countdown = gc.resolve_browser_event_kickoff(
            [
                {
                    "bwin_event_id": "7909713",
                    "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                    "text": "Argentina Costa Rica Comienza en 45 min",
                    "text_source": "SINGLE_EVENT_TILE",
                    "text_observed_at_utc": "2026-10-25T00:30:00+00:00",
                    "browser_timezone": "Europe/Madrid",
                }
            ],
            "7909713",
        )
        self.assertIsNotNone(dst_countdown)
        self.assertEqual(dst_countdown["kickoff_utc"].isoformat(), "2026-10-25T01:15:00+00:00")

        # Tampered kickoff_utc copied from API while observed_at_utc + countdown_minutes disagrees
        import match_identity as mi
        payload = _make_match_engine_json(
            "7909713", 1647372, 26, "Argentina", 29, "Costa Rica",
            "2026-11-17T23:00:00+00:00", "2026-11-17T22:03:45+00:00",
        )
        payload["match"]["temporal_identity_evidence"] = {
            "schema_version": "1.1-browser-event-time",
            "source": "SINGLE_EVENT_TILE",
            "method": "BWIN_EVENT_COUNTDOWN_MINUTES",
            "bwin_event_id": "7909713",
            "observed_at_utc": "2026-11-17T21:00:00+00:00",
            "countdown_minutes": 56,
            "kickoff_utc": "2026-11-17T23:00:00+00:00",  # tampered to match API instead of 21:56
            "catalog_observations": [
                {
                    "bwin_event_id": "7909713",
                    "bwin_url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "inconsistente"):
            mi.identity_from_payload(payload)


class TestP0BShadowSyncAndSnapshots(unittest.TestCase):
    def _stage_event(self, root: Path, event_id: str = "7909713") -> tuple[Path, Path, Path]:
        ev_dir = root / "outputs" / "matches" / event_id
        inputs = ev_dir / "inputs"
        sel = ev_dir / "selection"
        inputs.mkdir(parents=True, exist_ok=True)
        sel.mkdir(parents=True, exist_ok=True)

        ko_utc = "2026-11-17T23:00:00+00:00"
        ko_mad = "2026-11-18T00:00:00+01:00"
        ctx_json = inputs / f"match_engine_{event_id}.json"
        ctx_txt = inputs / f"match_engine_{event_id}.txt"
        odds_txt = inputs / f"cuotas_bwin_{event_id}.txt"
        ident_json = inputs / "match_identity.json"
        selected_json = sel / "selected_match.json"
        decision_json = sel / "selection_decision.json"

        real_dir = Path(__file__).resolve().parent.parent / "outputs" / "matches" / "7909713" / "context_updates" / "161fcb1298189e867aabbeca"
        real_ctx_json = real_dir / "match_engine_7909713.json"
        real_ctx_txt = real_dir / "match_engine_7909713.txt"
        if real_ctx_json.is_file() and real_ctx_txt.is_file() and event_id == "7909713":
            shutil.copy2(real_ctx_json, ctx_json)
            shutil.copy2(real_ctx_txt, ctx_txt)
        else:
            ctx_json.write_text(
                json.dumps(
                    _make_match_engine_json(
                        event_id, 1647372, 26, "Argentina", 29, "Costa Rica", ko_utc, "2026-11-17T21:10:00+00:00"
                    )
                ),
                encoding="utf-8",
            )
            ctx_txt.write_text(f"Event ID: {event_id}\nFixture ID: 1647372\n", encoding="utf-8")
        odds_txt.write_text(
            _make_odds_txt(
                event_id,
                "Argentina",
                "Costa Rica",
                "2026-11-17T22:10:00+01:00",
                home_odd=1.07,
                draw_odd=13.5,
                away_odd=26.0,
                extra_selections=449,
            ),
            encoding="utf-8",
        )
        ident_json.write_text(
            json.dumps(
                {
                    "schema_version": "1.0-fixture-bound",
                    "identity_status": "FIXTURE_AND_BWIN_TIME_VERIFIED",
                    "bwin_event_id": event_id,
                    "bwin_url": f"https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:{event_id}",
                    "api_fixture_id": 1647372,
                    "kickoff_utc": ko_utc,
                    "home": {"api_team_id": 26, "api_name": "Argentina", "bwin_name": "Argentina"},
                    "away": {"api_team_id": 29, "api_name": "Costa Rica", "bwin_name": "Costa Rica"},
                }
            ),
            encoding="utf-8",
        )
        selected_json.write_text(
            json.dumps(
                {
                    "bwin_event_id": event_id,
                    "fixture_id": 1647372,
                    "bwin_url": f"https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:{event_id}",
                    "home": "Argentina",
                    "away": "Costa Rica",
                    "kickoff_madrid": ko_mad,
                    "identity_status": "FIXTURE_AND_BWIN_TIME_VERIFIED",
                }
            ),
            encoding="utf-8",
        )
        decision_json.write_text(json.dumps({"event_id": event_id}), encoding="utf-8")

        case_doc = {
            "schema_version": "2.0-canonical-prematch-case",
            "event_id": event_id,
            "fixture_id": 1647372,
            "bwin_url": f"https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:{event_id}",
            "home": {"name": "Argentina", "api_team_id": 26},
            "away": {"name": "Costa Rica", "api_team_id": 29},
            "competition": {"league_id": 10, "name": "Friendlies", "season": 2026},
            "kickoff_utc": ko_utc,
            "kickoff_madrid": ko_mad,
            "identity_status": "FIXTURE_AND_BWIN_TIME_VERIFIED",
            "selected": {"path": str(selected_json), "sha256": _sha256(selected_json)},
            "source_handoff": {"path": str(decision_json), "sha256": _sha256(decision_json)},
        }
        (ev_dir / "case.json").write_text(json.dumps(case_doc), encoding="utf-8")

        manifest_doc = {
            "schema_version": "2.0-canonical-prematch-case",
            "event_id": event_id,
            "fixture_id": 1647372,
            "url": f"https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:{event_id}",
            "status": "PREPARADO",
            "inputs": {
                "context": {"path": str(ctx_txt), "sha256": _sha256(ctx_txt)},
                "context_json": {"path": str(ctx_json), "sha256": _sha256(ctx_json)},
                "odds": {"path": str(odds_txt), "sha256": _sha256(odds_txt)},
                "identity": {"path": str(ident_json), "sha256": _sha256(ident_json)},
            },
            "steps": {"scraper": 0},
        }
        (ev_dir / "manifest.json").write_text(json.dumps(manifest_doc), encoding="utf-8")
        return ev_dir, odds_txt, selected_json

    def test_expired_odds_excluded_in_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, _, _ = self._stage_event(root)
            res = run_shadow_for_event_dir(
                root,
                "7909713",
                historical=False,
                now_utc="2026-11-17T22:50:00+00:00",  # 100 min after 21:10 UTC capture
                max_odds_age_minutes=15,
                min_lead_minutes=0,
            )
            self.assertTrue(res["ok"])
            sh = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text())
            self.assertTrue(sh["odds_freshness_status"].startswith("EXPIRED_ODDS_EXCLUDED"))
            self.assertEqual(sh["odds_selection_count"], 0)
            self.assertTrue(
                all(r["odd"] is None and r["classification"] == "NO_ODDS" for r in sh["shadow_report"]["market_comparisons"])
            )

    def test_refresh_odds_updates_shadow_452_to_356_and_preserves_originals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, selected_json = self._stage_event(root)
            first_sha = _sha256(odds_txt)

            # 1) Run initial Shadow on first snapshot (452 selections, home @1.07)
            res1 = run_shadow_for_event_dir(
                root,
                "7909713",
                historical=False,
                now_utc="2026-11-17T21:12:00+00:00",
                max_odds_age_minutes=30,
                min_lead_minutes=0,
            )
            self.assertTrue(res1["ok"])
            sh1_path = ev_dir / "shadow" / "shadow_summary_7909713.json"
            sh1 = json.loads(sh1_path.read_text())
            self.assertEqual(sh1["odds_sha256"], first_sha)
            home1 = next(
                r for r in sh1["shadow_report"]["market_comparisons"]
                if r["market_family"] == "MATCH_RESULT_1X2" and r["selection_key"] == "home_win"
            )
            self.assertEqual(home1["odd"], 1.07)

            # 2) Refresh odds via prematch_single.cmd_refresh (356 selections, home @1.055)
            new_odds_txt = _make_odds_txt(
                "7909713",
                "Argentina",
                "Costa Rica",
                "2026-11-17T23:48:54+01:00",
                home_odd=1.055,
                draw_odd=14.5,
                away_odd=34.0,
                extra_selections=353,
            )

            def fake_run(cmd, cwd, env=None, log=None):
                odds_txt.write_text(new_odds_txt, encoding="utf-8")
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(f"TXT: {odds_txt}\n", encoding="utf-8")
                return 0

            ctx = {
                "root": root,
                "directory": ev_dir,
                "event_id": "7909713",
                "fixture_id": 1647372,
                "url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                "home": "Argentina",
                "away": "Costa Rica",
                "kickoff": datetime.fromisoformat("2026-11-18T00:00:00+01:00"),
                "selection": json.loads(selected_json.read_text()),
                "selected_path": selected_json,
                "selected_sha": _sha256(selected_json),
                "decision_path": ev_dir / "selection" / "selection_decision.json",
                "decision_sha": _sha256(ev_dir / "selection" / "selection_decision.json"),
            }
            args = SimpleNamespace(min_refresh_lead_minutes=5, max_odds_age_minutes=30, multi=False)
            with mock.patch("prematch_single.ensure_prematch"), \
                 mock.patch("prematch_single.ensure_root"), \
                 mock.patch("prematch_single.run", side_effect=fake_run), \
                 mock.patch("prematch_single.local_now", return_value=datetime.fromisoformat("2026-11-17T23:49:00+01:00")), \
                 mock.patch("prematch_single.iso_utc", return_value="2026-11-17T22:49:00+00:00"):
                rc = prematch_single.cmd_refresh(ctx, args)
            self.assertEqual(rc, 0)

            second_sha = _sha256(odds_txt)
            self.assertNotEqual(first_sha, second_sha)

            # Shadow must now reflect the second snapshot (@1.055), NOT @1.07
            sh2 = json.loads(sh1_path.read_text())
            self.assertEqual(sh2["odds_sha256"], second_sha)
            self.assertEqual(sh2["odds_captured_utc"], "2026-11-17T22:48:54+00:00")
            self.assertIsNotNone(sh2["probabilities_computed_at_utc"])
            self.assertIsNotNone(sh2["prices_evaluated_at_utc"])
            home2 = next(
                r for r in sh2["shadow_report"]["market_comparisons"]
                if r["market_family"] == "MATCH_RESULT_1X2" and r["selection_key"] == "home_win"
            )
            self.assertEqual(home2["odd"], 1.055)

            # Original snapshots (odds TXT and first shadow) must be preserved in archives/
            archived_odds = list((ev_dir / "archives").glob("refresh_*/cuotas_bwin_7909713.*.txt"))
            archived_shadow = list((ev_dir / "archives").glob("refresh_*/shadow/shadow_summary_7909713.json"))
            self.assertEqual(len(archived_odds), 1)
            self.assertEqual(_sha256(archived_odds[0]), first_sha)
            self.assertEqual(len(archived_shadow), 1)

            # Handoff dossier must expose explicit price and probability timestamps
            dossier_path = prematch_case.export_handoff(root, "7909713")
            dossier = json.loads(dossier_path.read_text())
            self.assertEqual(dossier["timestamps"]["odds_captured_utc"], "2026-11-17T22:48:54+00:00")
            self.assertEqual(dossier["timestamps"]["odds_sha256"], second_sha)
            self.assertFalse(dossier["timestamps"]["shadow_stale"])
            self.assertIsNotNone(dossier["timestamps"]["probabilities_computed_at_utc"])
            self.assertIsNotNone(dossier["timestamps"]["prices_evaluated_at_utc"])

    def test_blocked_historical_closure_cannot_become_final_prematch_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, _, _ = self._stage_event(root)
            manifest = json.loads((ev_dir / "manifest.json").read_text())
            manifest["status"] = "BLOQUEADO_VALIDACION"
            (ev_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            case = json.loads((ev_dir / "case.json").read_text())

            closure = prematch_case._evaluate_closure(
                ev_dir,
                case,
                manifest,
                now_override=datetime.fromisoformat("2026-11-17T22:50:00+00:00"),
            )
            self.assertFalse(closure["final_prematch_valid"])
            self.assertEqual(closure["closure_stage"], "PENDING_FINAL_VALIDATION")
            self.assertIn("MANIFEST_STATUS_BLOQUEADO_VALIDACION", closure["closure_reasons"])

    def test_shadow_failure_after_refresh_leaves_no_stale_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, selected_json = self._stage_event(root)
            res1 = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T21:12:00+00:00", max_odds_age_minutes=30, min_lead_minutes=0,
            )
            self.assertTrue(res1["ok"])
            sh_path = ev_dir / "shadow" / "shadow_summary_7909713.json"
            self.assertTrue(sh_path.is_file())

            # Refresh updates odds, then Shadow fails (e.g., corrupted/missing context_json)
            new_odds = _make_odds_txt(
                "7909713", "Argentina", "Costa Rica", "2026-11-17T23:48:54+01:00",
                home_odd=1.055, draw_odd=14.5, away_odd=34.0, extra_selections=353,
            )
            ctx = {
                "root": root,
                "directory": ev_dir,
                "event_id": "7909713",
                "fixture_id": 1647372,
                "url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                "home": "Argentina",
                "away": "Costa Rica",
                "kickoff": datetime.fromisoformat("2026-11-18T00:00:00+01:00"),
                "selection": json.loads(selected_json.read_text()),
                "selected_path": selected_json,
                "selected_sha": _sha256(selected_json),
                "decision_path": ev_dir / "selection" / "selection_decision.json",
                "decision_sha": _sha256(ev_dir / "selection" / "selection_decision.json"),
            }
            args = SimpleNamespace(min_refresh_lead_minutes=5, max_odds_age_minutes=30, multi=False)

            def fake_run(cmd, cwd, env=None, log=None):
                odds_txt.write_text(new_odds, encoding="utf-8")
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(f"TXT: {odds_txt}\n", encoding="utf-8")
                return 0

            with mock.patch("prematch_single.ensure_prematch"), \
                 mock.patch("prematch_single.ensure_root"), \
                 mock.patch("prematch_single.run", side_effect=fake_run), \
                 mock.patch("prematch_single.local_now", return_value=datetime.fromisoformat("2026-11-17T23:49:00+01:00")), \
                 mock.patch("prematch_single.iso_utc", return_value="2026-11-17T22:49:00+00:00"), \
                 mock.patch("bet_engine.orchestration.run_shadow_for_event_dir", side_effect=RuntimeError("SIMULATED_SHADOW_FAILURE")):
                rc = prematch_single.cmd_refresh(ctx, args)
            self.assertEqual(rc, 0)
            self.assertFalse(sh_path.is_file())
            se = json.loads((ev_dir / "single_execution.json").read_text())
            self.assertFalse(se["steps"]["refresh-odds"]["reevaluation"]["executed"])

    def test_two_consecutive_refreshes_keep_latest_active_and_archive_all(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, selected_json = self._stage_event(root)
            sha0 = _sha256(odds_txt)
            ctx = {
                "root": root,
                "directory": ev_dir,
                "event_id": "7909713",
                "fixture_id": 1647372,
                "url": "https://www.bwin.es/es/sports/eventos/argentina-costa-rica-2:7909713",
                "home": "Argentina",
                "away": "Costa Rica",
                "kickoff": datetime.fromisoformat("2026-11-18T00:00:00+01:00"),
                "selection": json.loads(selected_json.read_text()),
                "selected_path": selected_json,
                "selected_sha": _sha256(selected_json),
                "decision_path": ev_dir / "selection" / "selection_decision.json",
                "decision_sha": _sha256(ev_dir / "selection" / "selection_decision.json"),
            }
            args = SimpleNamespace(min_refresh_lead_minutes=5, max_odds_age_minutes=30, multi=False)

            def _do_refresh(cap_mad: str, now_mad: str, now_utc: str, odd_val: float, extra_n: int) -> str:
                txt = _make_odds_txt(
                    "7909713", "Argentina", "Costa Rica", cap_mad,
                    home_odd=odd_val, draw_odd=14.0, away_odd=30.0, extra_selections=extra_n,
                )
                def fake_run(cmd, cwd, env=None, log=None):
                    odds_txt.write_text(txt, encoding="utf-8")
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text(f"TXT: {odds_txt}\n", encoding="utf-8")
                    return 0
                with mock.patch("prematch_single.ensure_prematch"), \
                     mock.patch("prematch_single.ensure_root"), \
                     mock.patch("prematch_single.run", side_effect=fake_run), \
                     mock.patch("prematch_single.local_now", return_value=datetime.fromisoformat(now_mad)), \
                     mock.patch("prematch_single.iso_utc", return_value=now_utc):
                    rc = prematch_single.cmd_refresh(ctx, args)
                self.assertEqual(rc, 0)
                return _sha256(odds_txt)

            sha1 = _do_refresh("2026-11-17T23:25:00+01:00", "2026-11-17T23:25:10+01:00", "2026-11-17T22:25:10+00:00", 1.06, 400)
            sha2 = _do_refresh("2026-11-17T23:48:54+01:00", "2026-11-17T23:49:00+01:00", "2026-11-17T22:49:00+00:00", 1.055, 353)

            sh = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text())
            self.assertEqual(sh["odds_sha256"], sha2)
            home = next(
                r for r in sh["shadow_report"]["market_comparisons"]
                if r["market_family"] == "MATCH_RESULT_1X2" and r["selection_key"] == "home_win"
            )
            self.assertEqual(home["odd"], 1.055)
            archived = sorted((ev_dir / "archives").glob("refresh_*/cuotas_bwin_7909713.*.txt"))
            self.assertEqual({_sha256(p) for p in archived}, {sha0, sha1})

    def test_odds_manifest_hash_mismatch_and_stale_shadow_block_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, _ = self._stage_event(root)
            # Initial valid shadow
            res1 = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T21:12:00+00:00", max_odds_age_minutes=30, min_lead_minutes=0,
            )
            self.assertTrue(res1["ok"])
            sh_path = ev_dir / "shadow" / "shadow_summary_7909713.json"
            self.assertTrue(sh_path.is_file())

            # Tamper odds_txt on disk without updating manifest sha256 -> integrity block
            odds_txt.write_text(odds_txt.read_text() + "\n# tampered\n", encoding="utf-8")
            res2 = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T21:13:00+00:00", max_odds_age_minutes=30, min_lead_minutes=0,
            )
            self.assertFalse(res2["ok"])
            self.assertIn("ODDS_MANIFEST_SHA_MISMATCH", res2["error"])
            # Stale shadow must NOT remain active after failed integrity check
            self.assertFalse(sh_path.is_file())

            # Closure check must also reject mismatched odds/manifest or stale shadow even if FINALIZADO
            manifest = json.loads((ev_dir / "manifest.json").read_text())
            manifest["status"] = "FINALIZADO"
            manifest["finish_completed_at"] = "2026-11-17T22:50:00+00:00"
            case = json.loads((ev_dir / "case.json").read_text())
            closure = prematch_case._evaluate_closure(
                ev_dir, case, manifest,
                now_override=datetime.fromisoformat("2026-11-17T22:51:00+00:00"),
            )
            self.assertFalse(closure["final_prematch_valid"])
            self.assertIn("ODDS_MANIFEST_HASH_MISMATCH", closure["closure_reasons"])

    def test_refresh_near_kickoff_respects_prematch_lead_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev_dir, odds_txt, _ = self._stage_event(root)
            # At T-3 min (22:57 UTC for 23:00 UTC kickoff), min_lead_minutes=5 must block Shadow
            res = run_shadow_for_event_dir(
                root, "7909713", historical=False,
                now_utc="2026-11-17T22:57:00+00:00", max_odds_age_minutes=60, min_lead_minutes=5,
            )
            self.assertTrue(res["ok"])
            sh = json.loads((ev_dir / "shadow" / "shadow_summary_7909713.json").read_text())
            self.assertEqual(sh["shadow_report"]["status"], "BLOCKED_PREMATCH_GUARD")
            self.assertTrue(sh["shadow_report"]["live_data_blocked"])

            # ensure_prematch itself rejects T-3 min when min_lead=5
            ctx_check = {"kickoff": datetime.fromisoformat("2026-11-18T00:00:00+01:00")}
            with mock.patch("prematch_single.local_now", return_value=datetime.fromisoformat("2026-11-17T23:57:00+01:00")):
                with self.assertRaisesRegex(ValueError, "Margen 3.0 min, se requieren 5 min"):
                    prematch_single.ensure_prematch(ctx_check, min_lead=5)


if __name__ == "__main__":
    unittest.main()
