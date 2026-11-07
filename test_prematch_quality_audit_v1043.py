"""Offline tests of Phase 2.1; no API, no snapshots required."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import prematch_quality_audit_v1043 as qa

EVENT = "9876543"
FID = 90002
CUTOFF = "2026-11-05T12:00:00+00:00"
KICKOFF = "2026-11-05T19:00:00+00:00"
OLD_GAME = "2026-11-01T12:00:00+00:00"


def fixture(fid: int, tid: int, *, shot: int | None = 3):
    stats = [] if shot is None else [{"type": "Total Shots", "value": shot},
                                     {"type": "Corner Kicks", "value": 2}]
    return {"fixture": {"id": fid, "date": OLD_GAME,
                         "status": {"short": "FT"}},
            "teams": {"home": {"id": tid}, "away": {"id": 100 if tid != 100 else 200}},
            "statistics": [{"team": {"id": tid}, "statistics": stats}],
            "statistics_halves": [{"team": {"id": tid},
                                   "statistics": stats,
                                   "statistics_1h": [{"type": "Total Shots", "value": 1},
                                                     {"type": "Corner Kicks", "value": 1}],
                                   "statistics_2h": [{"type": "Total Shots", "value": 2},
                                                     {"type": "Corner Kicks", "value": 1}]}]}


def performance_row(fid: int, tid: int, *, shots: int | None = 3):
    return {"fixture_id": fid, "kickoff_utc": OLD_GAME,
            "goals_for": 1, "own": {"shots": shots, "on_target": 2,
                                    "corners": 2, "xg": 0.9},
            "conceded": {"shots": 4, "on_target": 1, "corners": 1, "xg": 0.7},
            "data_quality": {"status": "OK"}}


def make_documents(folder: Path, *, identity_status="FIXTURE_AND_BWIN_TIME_VERIFIED"):
    """Construct minimal compatible docs; issue counts depend on history length."""
    teams, pteams, tteams, dteams, plteams = {}, {}, {}, {}, {}
    for side, tid in (("home", 41), ("away", 42)):
        f = fixture(tid+1000, tid)
        r = performance_row(tid+1000, tid)
        teams[side+"_history_deep"] = {"detailed_fixtures": [f],
                                        "fixture_ids": [f["fixture"]["id"]],
                                        "completed_fixtures_count": 1}
        pteams[side] = {"team_id": tid, "team_name": side, "historical_fixtures": [r], "valid_count": 1}
        tteams[side] = {"team_id": tid, "observed_n": 1}
        dteams[side] = {"team_id": tid, "observed_n": 1}
        plteams[side] = {"team_id": tid, "historical_fixture_count": 1, "players": []}
    data = {
        "enriched": {"match": {"event_id": EVENT, "identity_status": identity_status},
                     "fixture": {"fixture_id": FID, "home_id": 41,
                                 "away_id": 42, "date": KICKOFF},
                     "generated_at": CUTOFF, "context": teams},
        "performance": {"event_id_bwin": EVENT, "target_fixture_id": FID,
                        "analysis_cutoff_utc": CUTOFF, "snapshot_generated_at_utc": CUTOFF,
                        "teams": pteams},
        "trends": {"event_id_bwin": EVENT, "target_fixture_id": FID,
                   "analysis_cutoff_utc": CUTOFF, "teams": tteams},
        "depth": {"event_id_bwin": EVENT, "target_fixture_id": FID,
                  "analysis_cutoff_utc": CUTOFF, "teams": dteams},
        "players": {"event_id_bwin": EVENT, "target_fixture_id": FID,
                    "cutoff_utc": CUTOFF, "teams": plteams},
    }
    names = {"enriched": "contexto_ampliado", "performance": "rendimiento_profundo",
             "trends": "tendencias_profundas", "depth": "profundidad_historica",
             "players": "jugadores_historicos"}
    folder.mkdir(parents=True)
    for name, doc in data.items():
        (folder / f"{names[name]}_{EVENT}.json").write_text(json.dumps(doc))
    (folder / f"auditoria_adquisicion_{EVENT}.json").write_text("{}")
    identity = folder.parent / "match_identity.json"
    identity.write_text(json.dumps({"bwin_event_id": EVENT, "api_fixture_id": FID,
                                    "kickoff_utc": KICKOFF, "identity_status": identity_status}))
    return identity, data


class Tests(unittest.TestCase):
    def with_docs(self):
        temp = tempfile.TemporaryDirectory()
        folder = Path(temp.name) / "deep_v1042"
        identity, docs = make_documents(folder)
        return temp, folder, identity, docs

    def test_real_sample_independent_if_present(self):
        root = Path(__file__).parent / "outputs/matches/7892145/analysis/deep_v1042"
        identity = root.parent.parent / "inputs/match_identity.json"
        if not root.exists() or not identity.exists():
            self.skipTest("Cruzeiro–Bahia ZIP no está instalado en entorno de pruebas")
        report = qa.audit_bundle(root, event_id="7892145", identity_path=identity)
        self.assertEqual(report["teams"]["home"]["history"]["raw"], 20)
        self.assertEqual(report["teams"]["away"]["coverage"]["L20"]["metrics"]["shots_for"]["observed_n"], 18)
        self.assertEqual(report["teams"]["home"]["halves"]["mismatch_n"], 3)
        self.assertEqual(report["teams"]["away"]["halves"]["mismatch_n"], 4)
        self.assertEqual(report["identity_status"], "API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN")
        self.assertFalse(report["final_with_bwin_odds_allowed"])

    def test_missing_values_are_not_zero(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            perf_file = folder / f"rendimiento_profundo_{EVENT}.json"
            raw = docs["performance"]
            raw["teams"]["home"]["historical_fixtures"][0]["own"]["shots"] = None
            perf_file.write_text(json.dumps(raw))
            audit = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            x = audit["teams"]["home"]["coverage"]["L5"]["metrics"]["shots_for"]
            self.assertEqual(x["observed_n"], 0)
            self.assertEqual(x["status"], "UNAVAILABLE")

    def test_zero_is_an_observation(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            raw = docs["performance"]
            raw["teams"]["home"]["historical_fixtures"][0]["own"]["shots"] = 0
            (folder / f"rendimiento_profundo_{EVENT}.json").write_text(json.dumps(raw))
            audit = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            self.assertEqual(audit["teams"]["home"]["coverage"]["L5"]["metrics"]["shots_for"]["observed_n"], 1)

    def test_target_fixture_not_in_history(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            src = docs["enriched"]
            src["context"]["home_history_deep"]["detailed_fixtures"][0]["fixture"]["id"] = FID
            (folder / f"contexto_ampliado_{EVENT}.json").write_text(json.dumps(src))
            audit = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            self.assertEqual(audit["quality_status"], "BLOCKED")
            self.assertIn("WRONG_HISTORICAL_FIXTURE", [i["code"] for i in audit["issues"]])

    def test_late_cutoff_blocks(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            data = docs["enriched"]
            data["generated_at"] = "2026-11-05T20:00:00+00:00"
            (folder / f"contexto_ampliado_{EVENT}.json").write_text(json.dumps(data))
            audit = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            self.assertEqual(audit["quality_status"], "BLOCKED")
            self.assertIn("TIME_BOUNDARY_VIOLATION", [i["code"] for i in audit["issues"]])

    def test_h1_h2_discrepancy_quarantined(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            enriched = docs["enriched"]
            half = enriched["context"]["home_history_deep"]["detailed_fixtures"][0]["statistics_halves"][0]
            half["statistics_2h"][0]["value"] = 5
            (folder / f"contexto_ampliado_{EVENT}.json").write_text(json.dumps(enriched))
            audit = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            self.assertEqual(audit["teams"]["home"]["halves"]["mismatch_n"], 1)
            self.assertEqual(audit["teams"]["home"]["halves"]["mismatches"][0]["metric"], "Total Shots")

    def test_stale_metadata_flagged(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            e = docs["enriched"]
            e["context"]["home_history_deep"]["fixture_ids"] = []
            (folder / f"contexto_ampliado_{EVENT}.json").write_text(json.dumps(e))
            audit = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            self.assertIn("LEGACY_HISTORY_METADATA_NOT_SYNCED", [i["code"] for i in audit["issues"]])

    def test_provisional_identity_never_enables_odds(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            ident = json.loads(identity.read_text())
            ident["identity_status"] = "API_FIXTURE_VERIFIED_BWIN_TIME_UNKNOWN"
            identity.write_text(json.dumps(ident))
            e = docs["enriched"]
            e["match"]["identity_status"] = ident["identity_status"]
            (folder / f"contexto_ampliado_{EVENT}.json").write_text(json.dumps(e))
            audit = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            self.assertFalse(audit["final_with_bwin_odds_allowed"])
            self.assertIn("BWIN_TIME_LINK_NOT_VERIFIED", [i["code"] for i in audit["issues"]])

    def test_player_denominator_violation_blocks(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            p = docs["players"]
            p["teams"]["home"]["players"] = [{"played_n": 1, "metrics": {
                "shots_total": {"observed_n": 2, "hit_1plus_n": 0,
                                "played_n": 1, "hist_fixture_ids": [1041, 1041]}}}]
            (folder / f"jugadores_historicos_{EVENT}.json").write_text(json.dumps(p))
            audit = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            self.assertEqual(audit["quality_status"], "BLOCKED")
            self.assertIn("PLAYER_EVIDENCE_DENOMINATOR_INVALID", [i["code"] for i in audit["issues"]])

    def test_no_input_mutation(self):
        tmp, folder, identity, docs = self.with_docs()
        with tmp:
            files = list(folder.glob("*.json")) + [identity]
            before = {str(f): qa.hash_file(f) for f in files}
            report = qa.audit_bundle(folder, event_id=EVENT, identity_path=identity)
            rendered = qa.markdown(report)
            after = {str(f): qa.hash_file(f) for f in files}
            self.assertEqual(before, after)
            self.assertIn("Auditoría PREMATCH", rendered)


if __name__ == "__main__":
    unittest.main()
