import argparse
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import prematch_case as case


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class CanonicalCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.event = "7912964"; self.fixture = 1549868
        self.directory = self.root / "outputs/matches" / self.event
        self.odds = self.directory / "inputs" / f"cuotas_bwin_{self.event}.txt"
        kickoff = (datetime.now(timezone.utc) + timedelta(hours=3)).astimezone(ZoneInfo("Europe/Madrid"))
        capture = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.url = f"https://www.bwin.es/es/sports/eventos/deportivo-pasto-cd-junior-fc-barranquilla-2:{self.event}"
        self.odds.parent.mkdir(parents=True)
        self.odds.write_text(f"Event ID: {self.event}\nURL: {self.url}\nCaptura: {capture.isoformat()}\n", encoding="utf-8")
        self.selection_dir = self.root / "selection"
        self.selected = self.selection_dir / "selected_match.json"
        decision = self.selection_dir / "selection_decision.json"
        selection = {
            "schema_version": "1.0-selected-single-fixture", "status": "SELECTED_FOR_DEEP_RESEARCH",
            "fixture_id": self.fixture, "bwin_event_id": self.event, "bwin_url": self.url,
            "home": "Deportivo Pasto", "away": "Junior", "home_api_id": 1, "away_api_id": 2,
            "league_id": 239, "league": "Primera A", "season": 2026,
            "kickoff_madrid": kickoff.isoformat(), "selection_asof_madrid": capture.isoformat(),
            "market_families_ready_for_research": ["goals"], "selection_tier": "A",
            "research_eligible": True, "eligible": True, "research_missing_evidence": [], "hard_block_reasons": [],
            "odds_txt": str(self.odds), "odds_sha256": hashlib.sha256(self.odds.read_bytes()).hexdigest(),
        }
        winner = {**selection, "market_families_ready": ["goals"]}
        dump(self.selected, selection)
        dump(decision, {"status": "SELECTED", "provisional_or_selected": winner})
        self.identity_audit = self.root / "identity_audit.json"
        dump(self.identity_audit, {"schema_version": "1.0-bwin-identity-audit", "fixtures": [{
            "fixture_id": self.fixture, "bwin_event_id": self.event, "link_status": "VERIFIED_LINK"}]})
        audit_sha = hashlib.sha256(self.identity_audit.read_bytes()).hexdigest()
        self.handoff = self.root / "handoff.json"; dump(self.handoff, {
            "fixture_id": self.fixture, "bwin_event_id": self.event,
            "identity_coverage_audit": {"path": str(self.identity_audit), "sha256": audit_sha}})

    def tearDown(self): self.tmp.cleanup()

    def adopt(self):
        with patch("prematch_case.prematch_release.bridge", return_value=self.selected):
            return case.adopt(self.root, self.handoff)

    def test_daily_handoff_becomes_existing_operational_manifest(self):
        selected = self.adopt()
        self.assertEqual(selected, self.selected)
        manifest = json.loads((self.directory / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "ERROR_CONTEXTO")
        self.assertEqual(manifest["next_action"], "retry-context")
        self.assertEqual(manifest["inputs"]["odds"]["sha256"], hashlib.sha256(self.odds.read_bytes()).hexdigest())
        self.assertEqual(case.status(self.root, self.event)["fixture_id"], self.fixture)
        canonical = json.loads((self.directory / "case.json").read_text())
        self.assertEqual(canonical["identity_coverage_audit"]["sha256"],
                         hashlib.sha256(self.identity_audit.read_bytes()).hexdigest())

    def test_adoption_is_idempotent_and_never_overwrites_a_conflict(self):
        self.adopt(); self.adopt()
        path = self.directory / "case.json"
        original = path.read_bytes(); path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "WRITE_ONCE_CONFLICT"):
            self.adopt()
        self.assertNotEqual(path.read_bytes(), original)

    def test_manual_hypotheses_are_identity_checked_and_preserved(self):
        self.adopt()
        source = self.root / "hypotheses.json"
        dump(source, {"schema_version": case.HYPOTHESIS_SCHEMA, "event_id": self.event,
                      "fixture_id": self.fixture, "hypotheses": [{"id": "h1", "variables": ["shots"],
                      "evidence": [1], "conditions": {}, "refutation": "No holdout effect",
                      "prematch_availability": "HISTORICAL_AGGREGATE"}]})
        target = case.attach_hypotheses(self.root, self.event, source)
        self.assertTrue(target.is_file())
        manifest = json.loads((self.directory / "manifest.json").read_text())
        self.assertEqual(manifest["hypothesis_validation_status"], "PENDING_PYTHON_VALIDATION")
        bad = json.loads(source.read_text()); bad["event_id"] = "9999999"; dump(source, bad)
        with self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
            case.attach_hypotheses(self.root, self.event, source)

    def test_dossier_fails_closed_on_changed_input(self):
        self.adopt()
        dossier = case.export_handoff(self.root, self.event)
        self.assertTrue(dossier.is_file())
        self.odds.write_text(self.odds.read_text() + "changed")
        with self.assertRaisesRegex(ValueError, "HASH_INVALID"):
            case.export_handoff(self.root, self.event)

    def test_python_validation_writes_all_auditable_artifacts(self):
        self.adopt()
        source = self.root / "hypotheses.json"
        dump(source, {"schema_version": case.HYPOTHESIS_SCHEMA, "event_id": self.event,
                      "fixture_id": self.fixture, "hypotheses": [{"id": "h1", "variables": ["shots"],
                      "evidence": [1], "conditions": {}, "mechanism": "pressure", "refutation": "no effect",
                      "prematch_availability": "HISTORICAL_AGGREGATE", "python_test": {"segment": {}}}]})
        case.attach_hypotheses(self.root, self.event, source)
        packet = self.root / "packet.json"; sealed = self.root / "sealed.json"
        dump(packet, {"targets": [{"fixture_id": self.fixture}]}); dump(sealed, {})
        result = {"schema_version": "1.0-llm-pattern-validation", "results": [{"hypothesis": {"hypothesis_id": "h1"},
                  "status": "REJECTED", "reason": "INSUFFICIENT_SAMPLE"}], "actionable_bet": False}
        with patch("prematch_llm_research.validate_hypotheses", return_value=result):
            folder = case.validate_hypotheses(self.root, self.event, packet, sealed)
        self.assertEqual({p.name for p in folder.iterdir()}, {"llm_pattern_hypotheses.json", "pattern_validation.json",
                         "pattern_registry.json", "ranking_enriched.json"})
        manifest = json.loads((self.directory / "manifest.json").read_text())
        self.assertEqual(manifest["hypothesis_validation_status"], "PYTHON_VALIDATION_COMPLETE")

    def test_historical_selection_pair_is_readable_but_not_actionable(self):
        selection = json.loads(self.selected.read_text()); decision_path = self.selection_dir / "selection_decision.json"
        decision = json.loads(decision_path.read_text())
        selection["status"] = "HISTORICAL_AUDIT_ONLY"; decision["status"] = "HISTORICAL_AUDIT_ONLY"
        dump(self.selected, selection); dump(decision_path, decision)
        ctx = case.selected_context(self.root, self.selected)
        self.assertEqual(ctx["event_id"], self.event)
        self.assertFalse(ctx["selection"].get("actionable_bet", False))


class FrozenPastoRegressionTests(unittest.TestCase):
    def test_frozen_pasto_junior_identity_remains_exact(self):
        root = Path(__file__).resolve().parents[1]
        reconciliation = root / "outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json"
        if not reconciliation.is_file(): self.skipTest("frozen reconciliation artifact unavailable")
        document = json.loads(reconciliation.read_text(encoding="utf-8"))
        serialized = json.dumps(document, ensure_ascii=False)
        self.assertIn('"fixture_id": 1549868', serialized)
        self.assertIn('"event_id": "7912964"', serialized)
        identity = root / "outputs/data/match_identity_7912964.json"
        record = json.loads(identity.read_text(encoding="utf-8"))
        self.assertEqual(str(record["bwin_event_id"]), "7912964")
        self.assertEqual(int(record["api_fixture_id"]), 1549868)
        self.assertNotEqual(record["home"]["api_team_id"], record["away"]["api_team_id"])


if __name__ == "__main__": unittest.main()
