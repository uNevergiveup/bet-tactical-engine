import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import bwin_discovery as bwin
import competition_registry as registry
import filter_major_leagues as fml
import prematch_prospective_trial as trial
import preselect_matches as presel
from bet_engine.contracts.multi_slot import MultiMatchExecutionState
from bet_engine.orchestration.multi_orchestrator import (
    MultiSlotOrchestrator,
    OrchestratorConfig,
    resolve_or_generate_preselection,
)


def _l10_row(fid: int, home: str, away: str, kickoff: str = "2026-11-17T23:00:00+00:00", round_name: str = "Friendly International") -> dict:
    return {
        "fixture_id": fid,
        "kickoff_utc": kickoff,
        "kickoff_madrid": datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(trial.TZ).isoformat(),
        "country": "World",
        "league": "Friendlies",
        "league_id": 10,
        "season": 2026,
        "league_type": None,
        "round": round_name,
        "home": home,
        "home_id": fid * 10 + 1,
        "away": away,
        "away_id": fid * 10 + 2,
        "status": "NS",
    }


class TestSeniorNationalFriendliesV3(unittest.TestCase):
    DAY = date(2026, 11, 17)
    LABEL = "2026-11-17_1500_0300"

    def test_senior_friendlies_pass_while_youth_and_ambiguous_stay_blocked(self):
        senior_pairs = [
            (1647369, "Qatar", "South Korea", "2026-11-17T14:00:00+00:00"),
            (1650001, "United Arab Emirates", "Syria", "2026-11-17T14:10:00+00:00"),
            (1647370, "Saudi Arabia", "Uzbekistan", "2026-11-17T15:30:00+00:00"),
            (1640089, "Jordan", "Australia", "2026-11-17T16:00:00+00:00"),
            (1647371, "Russia", "Venezuela", "2026-11-17T16:00:00+00:00"),
            (1649550, "Iraq", "Iran", "2026-11-17T16:15:00+00:00"),
            (1647643, "Kuwait", "Honduras", "2026-11-17T16:30:00+00:00"),
            (1640519, "Chile", "Colombia", "2026-11-17T23:00:00+00:00"),
            (1647372, "Argentina", "Costa Rica", "2026-11-17T23:00:00+00:00"),
            (1647373, "Ecuador", "Bolivia", "2026-11-17T23:30:00+00:00"),
            (1648287, "Uruguay", "Dominican Republic", "2026-11-17T23:30:00+00:00"),
        ]
        youth_pairs = [
            (1649023, "Turkey U18", "Germany U18"),
            (1649027, "Portugal U21", "Norway U21"),
            (1649028, "Albania U21", "Spain U21"),
        ]
        ambiguous_or_non_senior = [
            (99001, "Spain B", "Italy B", "Friendly International"),
            (99002, "Brazil Olympic", "Japan Olympic", "Friendly International"),
            (99003, "Morocco Local", "Senegal A'", "Friendly International"),
            (99004, "Leganes", "Burgos", "Friendly International"),
            (99005, "Argentina Women", "Chile", "Friendly International"),
            (99006, "Spain", "France", "Youth Friendly U20"),
        ]
        women_senior = _l10_row(99007, "Brazil Women", "USA Women")

        fixtures = (
            [_l10_row(fid, h, a, ko) for fid, h, a, ko in senior_pairs]
            + [_l10_row(fid, h, a) for fid, h, a in youth_pairs]
            + [_l10_row(fid, h, a, round_name=rnd) for fid, h, a, rnd in ambiguous_or_non_senior]
            + [women_senior]
        )
        doc = {"schema_version": "1.0-discovery", "fixtures": fixtures}
        raw_bytes = json.dumps(doc).encode("utf-8")
        res = fml.filter_document(doc, raw_bytes, policy="adult-open")

        elig_by_id = {x["fixture_id"]: x for x in res["fixtures"]}
        for fid, _, _, _ in senior_pairs:
            self.assertIn(fid, elig_by_id)
            item = elig_by_id[fid]
            self.assertEqual(item["filter_status"], "ELIGIBLE")
            self.assertEqual(item["filter_reason"], "NATIONAL_FRIENDLY")
            self.assertEqual(item["gender"], "M")
            self.assertEqual(item["category_level"], "SENIOR")
            self.assertEqual(item["category_evidence_status"], "CORROBORATED")
            self.assertEqual(item["matching_eligibility"], "ELIGIBLE")
            self.assertEqual(item["coverage_exception_reason"], "NATIONAL_FRIENDLY_NO_STANDINGS_FORMAT")

        self.assertIn(99007, elig_by_id)
        self.assertEqual(elig_by_id[99007]["gender"], "F")

        exc_ids = {x["fixture_id"] for x in res["excluded"]}
        for fid, _, _ in youth_pairs:
            self.assertIn(fid, exc_ids)
        self.assertIn(99001, exc_ids)  # B teams excluded

        rev_ids = {x["fixture_id"] for x in res["review"]}
        for blocked_fid in (99002, 99003, 99004, 99005, 99006):
            self.assertNotIn(blocked_fid, elig_by_id)
            self.assertIn(blocked_fid, rev_ids | exc_ids)

        # Bwin similarity sigue exigiendo etiqueta compatible de amistoso internacional (no clubes)
        self.assertEqual(
            bwin.competition_similarity("Friendlies", "Amistosos Internacionales Chile Colombia", league_id=10, country="World"),
            1.0,
        )
        self.assertEqual(
            bwin.competition_similarity("Friendlies", "Amistosos Clubes", league_id=10, country="World"),
            0.0,
        )

    def test_preselect_does_not_require_standings_but_keeps_history_and_sample_gates(self):
        # Muestras estadísticas válidas sin standings (standings='FALSE', statistics_fixtures='TRUE')
        obs = [
            {
                "fixture_id": 500 + i,
                "kickoff_utc": f"2026-10-{10 + i:02d}T18:00:00+00:00",
                "acquired_at_utc": f"2026-10-{11 + i:02d}T00:00:00+00:00",
                "league_id": 10,
                "season": 2026,
                "status": "FT",
                "own": {"goals": 2, "corners": 5, "shots": 12, "yellow": 2},
                "conceded": {"goals": 1, "corners": 4, "shots": 8, "yellow": 1},
            }
            for i in range(4)
        ]
        good_match = {
            "fixture_id": 1647372,
            "league_id": 10,
            "season": 2026,
            "analysis_cutoff_utc": "2026-11-17T22:00:00+00:00",
            "competition_eligibility_reason": "NATIONAL_FRIENDLY",
            "coverage_exception_reason": "NATIONAL_FRIENDLY_NO_STANDINGS_FORMAT",
            "research_priority_score": 68.0,
            "competition_coverage": {
                "league_id": 10,
                "season": 2026,
                "status": "DECLARED",
                "competition_type": "Cup",
                "capabilities": {
                    "statistics_fixtures": "TRUE",
                    "standings": "FALSE",
                    "events": "TRUE",
                    "lineups": "TRUE",
                    "statistics_players": "TRUE",
                    "players": "TRUE",
                },
            },
            "statistical_observations": {
                "home": obs,
                "away": obs,
            },
        }
        # Aunque competition_type sea 'League' y standings='FALSE', la exención explícita de amistoso debe aplicar
        good_match["competition_coverage"]["competition_type"] = "League"
        presel.apply_coverage_priority(good_match)
        sp = good_match["statistical_priority"]
        self.assertEqual(sp["category"], "ADVANCED")
        self.assertEqual(sp["exception_reason"], "NATIONAL_FRIENDLY_NO_STANDINGS_FORMAT")
        self.assertIn("DOCUMENTED_NO_TABLE_EXCEPTION", sp["reasons"])

        # Cobertura histórica insuficiente sin muestras ni detalle sigue degradada (SECONDARY, 0 bonus)
        weak_match = {
            **good_match,
            "statistical_observations": {"home": obs[:1], "away": []},
        }
        presel.apply_coverage_priority(weak_match)
        self.assertEqual(weak_match["statistical_priority"]["category"], "SECONDARY")
        self.assertEqual(weak_match["statistical_priority"]["bonus"], 0)

    def test_stale_20_snapshot_preserved_and_versioned_evaluation_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for script in (
                "discover_fixtures.py",
                "filter_major_leagues.py",
                "preselect_matches.py",
                "prematch_pattern_engine.py",
                "prematch_pattern_discovery.py",
                "bwin_discovery.py",
            ):
                (root / script).write_text("# stub\n", encoding="utf-8")

            win_dir = root / "outputs" / "discovery" / self.LABEL
            base = win_dir / "prospective_trial"
            base.mkdir(parents=True)

            # Snapshot antiguo congelado con filter_implementation 2.0-adult-open
            old_fixtures = [_l10_row(1647372, "Argentina", "Costa Rica", "2026-11-17T23:00:00+00:00")]
            trial.dump(win_dir / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": old_fixtures})
            old_raw_sha = trial.sha(win_dir / "fixtures.json")
            trial.dump(
                win_dir / "fixtures_major.json",
                {"schema_version": "1.0-major-leagues", "filter_implementation": "2.0-adult-open", "source_sha256": old_raw_sha, "fixtures": []},
            )
            old_major_sha = trial.sha(win_dir / "fixtures_major.json")
            trial.dump(
                base / "fixtures_major_test.json",
                {"schema_version": "1.0-major-leagues", "fixtures": [], "trial_subset": {"source_sha256": old_major_sha, "count": 0}},
            )
            from prematch_audit import CandidateLedger
            CandidateLedger(base / "candidate_ledger.json", {"horizon_hours": 0}).write()

            enriched_fixture = {
                **old_fixtures[0],
                "filter_status": "ELIGIBLE",
                "filter_reason": "NATIONAL_FRIENDLY",
                "gender": "M",
                "region": "OTHER",
                "region_priority": 3,
                "data_readiness_score": 82,
                "research_priority_score": 78.0,
                "preliminary_coverage_score": 75,
                "details_checked": True,
            }

            def fake_trial_run(r: Path, script: str, args: list[str], logs: Path) -> Path:
                if script == "discover_fixtures.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(out_dir / "fixtures.json", {"schema_version": "1.0-discovery", "fixtures": old_fixtures, "fixture_audit": old_fixtures})
                elif script == "filter_major_leagues.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    res = fml.filter_document(trial.read(out_dir / "fixtures.json"), (out_dir / "fixtures.json").read_bytes(), policy="adult-open")
                    fml.save_outputs(res, out_dir)
                elif script == "preselect_matches.py":
                    in_path = Path(args[args.index("--input") + 1])
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(
                        out_dir / "preselection.json",
                        {
                            "schema_version": "1.3-preselection",
                            "generated_at_utc": "2026-11-17T14:40:00+00:00",
                            "source_filter_sha256": trial.sha(in_path),
                            "methodology": {"sample_history_cap": 20, "sample_details_cap": 8},
                            "counts": {"input_filter": 1},
                            "api_request_ledger": [],
                            "matches": [enriched_fixture],
                        },
                    )
                elif script == "prematch_pattern_discovery.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(out_dir / "pattern_discovery.json", {})
                    trial.dump(out_dir / "research_queue_sport.json", {})
                elif script == "prematch_pattern_engine.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    pre_p = Path(args[args.index("--preselection") + 1])
                    trial.dump(out_dir / "pattern_engine.json", {"data_sources": {"preselection_sha256": trial.sha(pre_p)}, "fixtures": []})
                elif script == "bwin_discovery.py":
                    out_dir = Path(args[args.index("--output-dir") + 1])
                    trial.dump(
                        out_dir / "bwin_discovery.json",
                        {
                            "generated_at_utc": "2026-11-17T14:41:00+00:00",
                            "catalog_observed": [],
                            "matches": [{"fixture_id": 1647372, "status": "POSSIBLE_EVENT_FOUND", "bwin_event_id": 7915001}],
                        },
                    )
                return logs / f"{script}.log"

            def fake_save_report(pre_doc: dict, staging: Path) -> None:
                staging.mkdir(parents=True, exist_ok=True)
                trial.dump(staging / "preselection.json", pre_doc)
                (staging / "preselection.csv").write_text("fixture_id\n", encoding="utf-8")
                (staging / "preselection.md").write_text("# Pre\n", encoding="utf-8")

            cfg = OrchestratorConfig(window_day=self.DAY, offline=False, online=True, confirm_external=True)
            with patch.object(trial, "run", side_effect=fake_trial_run), \
                 patch("preselect_matches.save_report", side_effect=fake_save_report), \
                 patch("prematch_tactical_signal_engine.run_engine", return_value={"fixtures": {"1647372": {"priority_score": 78.0}}}), \
                 patch("bwin_identity_audit.audit", return_value={"summary": {}, "matches": []}), \
                 patch("bwin_identity_audit.write_reports"):
                pre_path, auto_gen = resolve_or_generate_preselection(
                    root, cfg, now_utc=datetime(2026, 11, 17, 13, 45, tzinfo=timezone.utc)
                )

            self.assertTrue(auto_gen)
            # Debe abrir nueva evaluación versionada bajo evaluations/ sin tocar el snapshot antiguo congelado
            self.assertIn("evaluations", str(pre_path))
            self.assertEqual(trial.sha(win_dir / "fixtures.json"), old_raw_sha)
            self.assertEqual(trial.sha(win_dir / "fixtures_major.json"), old_major_sha)

    def test_e2e_offline_ranking_assigns_franja_c_and_filters_low_coverage_or_missing_bwin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eval_base = root / "outputs" / "discovery" / self.LABEL / "evaluations" / "20261117T134500Z_abc" / "prospective_trial"
            pre_path = eval_base / "enriched" / "preselection.json"
            bwin_path = eval_base / "bwin" / "bwin_discovery.json"
            pre_path.parent.mkdir(parents=True, exist_ok=True)
            bwin_path.parent.mkdir(parents=True, exist_ok=True)

            matches = [
                # Franja A apto
                {**_l10_row(1647370, "Saudi Arabia", "Uzbekistan", "2026-11-17T15:30:00+00:00"), "data_readiness_score": 78, "research_priority_score": 72.0, "details_checked": True},
                # Franja A cobertura insuficiente
                {**_l10_row(1647643, "Kuwait", "Honduras", "2026-11-17T16:30:00+00:00"), "data_readiness_score": 28, "research_priority_score": 15.0, "details_checked": True},
                # Franja B liga regular apta
                {"fixture_id": 1555001, "league_id": 141, "league": "Segunda División", "country": "Spain", "season": 2026, "home": "Club H", "away": "Club A", "kickoff_utc": "2026-11-17T18:30:00+00:00", "data_readiness_score": 82, "research_priority_score": 76.0, "details_checked": True},
                # Franja C amistoso absoluto apto con Bwin
                {**_l10_row(1647372, "Argentina", "Costa Rica", "2026-11-17T23:00:00+00:00"), "data_readiness_score": 86, "research_priority_score": 84.0, "details_checked": True},
                # Franja C amistoso absoluto SIN Bwin
                {**_l10_row(1640519, "Chile", "Colombia", "2026-11-17T23:00:00+00:00"), "data_readiness_score": 88, "research_priority_score": 91.0, "details_checked": True},
            ]
            trial.dump(pre_path, {"generated_at_utc": "2026-11-17T13:40:00+00:00", "matches": matches})
            trial.dump(
                bwin_path,
                {
                    "matches": [
                        {"fixture_id": 1647370, "status": "POSSIBLE_EVENT_FOUND", "bwin_event_id": 7911001},
                        {"fixture_id": 1647643, "status": "POSSIBLE_EVENT_FOUND", "bwin_event_id": 7911002},
                        {"fixture_id": 1555001, "status": "POSSIBLE_EVENT_FOUND", "bwin_event_id": 7911003},
                        {"fixture_id": 1647372, "status": "POSSIBLE_EVENT_FOUND", "bwin_event_id": 7911004},
                        {"fixture_id": 1640519, "status": "NOT_FOUND_IN_SCANNED_PAGES", "bwin_event_id": None},
                    ]
                },
            )

            orch = MultiSlotOrchestrator(root, OrchestratorConfig(window_day=self.DAY, require_bwin=True))
            plan = orch.init_or_load_plan(now_utc=datetime(2026, 11, 17, 13, 45, tzinfo=timezone.utc))

            c_arg = plan.get_candidate(1647372)
            self.assertEqual(c_arg.slot_id, "FRANJA_C")
            self.assertEqual(c_arg.eligibility_status, "ELIGIBLE")
            self.assertEqual(c_arg.execution_state, MultiMatchExecutionState.RANKED)
            self.assertEqual(c_arg.global_rank, 1)
            self.assertEqual(c_arg.event_id, 7911004)

            c_chi = plan.get_candidate(1640519)
            self.assertEqual(c_chi.slot_id, "FRANJA_C")
            self.assertEqual(c_chi.eligibility_status, "INELIGIBLE_NO_BWIN")
            self.assertEqual(c_chi.execution_state, MultiMatchExecutionState.SKIPPED_INELIGIBLE)

            c_kuw = plan.get_candidate(1647643)
            self.assertEqual(c_kuw.eligibility_status, "INELIGIBLE_DATA_QUALITY")
            self.assertEqual(c_kuw.execution_state, MultiMatchExecutionState.SKIPPED_INELIGIBLE)


if __name__ == "__main__":
    unittest.main()
