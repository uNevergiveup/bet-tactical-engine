# REGRESIONES — Comparación exacta antes y después de P0 (Iteración 1.1)

## 1. Resumen cuantitativo

| Ejecución | Comando | Tests ejecutados | Duración | Resumen |
|---|---|---:|---:|---|
| Pre-P0 (Iteración 1, antes de cualquier edición, `task-12`) | `.venv/bin/python -m unittest discover -s test -p "test_*.py"` | 984 | 56.711s | `failures=17, errors=37, skipped=17` |
| Post-P0 (Iteración 1.1, tras endurecimiento P0, `task-324`) | `.venv/bin/python -m unittest discover -s test -p "test_*.py"` | 997 | 56.062s | `failures=17, errors=37, skipped=17` |

- Incremento neto de tests ejecutados: **+13** (los 13 tests de `test/test_p0_temporal_and_shadow_sync.py`, 13/13 `OK`).
- **Nuevos FAIL respecto al baseline Pre-P0:** `0` (`[]`)
- **Nuevos ERROR respecto al baseline Pre-P0:** `0` (`[]`)
- **FAIL resueltos:** `0` | **ERROR resueltos:** `0`
- **Igualdad exacta de conjuntos:** `set(pre_fails) == set(post_fails)` (17/17) y `set(pre_errors) == set(post_errors)` (37/37).

## 2. Los 17 FAIL preexistentes (idénticos antes y después)

1. `test_absent_XI_keeps_context_and_archives_acquisition (test_p0_repair.LineupAcquisitionTests.test_absent_XI_keeps_context_and_archives_acquisition)` — **PREEXISTENTE**
2. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='Africa Cup of Nations')` — **PREEXISTENTE**
3. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='Club Friendlies')` — **PREEXISTENTE**
4. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='Copa del Rey')` — **PREEXISTENTE**
5. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='Europa League')` — **PREEXISTENTE**
6. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='FA Cup')` — **PREEXISTENTE**
7. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='National 2')` — **PREEXISTENTE**
8. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='Primera Nacional')` — **PREEXISTENTE**
9. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='Segunda División')` — **PREEXISTENTE**
10. `test_adult_champions_cups_and_lower_divisions (test_filter_adult_competitions.AdultCompetitionsTest.test_adult_champions_cups_and_lower_divisions) (label='UEFA Champions League')` — **PREEXISTENTE**
11. `test_leganes_category_propagates_through_real_contracts (test_filter_adult_competitions.AdultCompetitionsTest.test_leganes_category_propagates_through_real_contracts)` — **PREEXISTENTE**
12. `test_normal_club_names_not_false_positive (test_filter_adult_competitions.AdultCompetitionsTest.test_normal_club_names_not_false_positive)` — **PREEXISTENTE**
13. `test_output_schema_compatible_and_audited (test_filter_adult_competitions.AdultCompetitionsTest.test_output_schema_compatible_and_audited)` — **PREEXISTENTE**
14. `test_prepare_checks_before_launch_and_normal_mode_is_unchanged (test_prematch_reuse_odds_contract.ReuseTests.test_prepare_checks_before_launch_and_normal_mode_is_unchanged)` — **PREEXISTENTE**
15. `test_real_failed_evaluation_offline_replay_activates_scanner_and_research_pending (test_fase_a_b_recovery_quick_context.TestFaseARecovery.test_real_failed_evaluation_offline_replay_activates_scanner_and_research_pending)` — **PREEXISTENTE**
16. `test_restored_mtime_cannot_refresh_cache (test_prematch_functional_isolation.CacheTests.test_restored_mtime_cannot_refresh_cache)` — **PREEXISTENTE**
17. `test_v3_budget_classifies_before_external_work (test_bwin_discovery_v3.DiscoveryV3Tests.test_v3_budget_classifies_before_external_work)` — **PREEXISTENTE**

## 3. Los 37 ERROR preexistentes (idénticos antes y después)

1. `setUpClass (test_prematch_methodology_4b1.MethodologyTests)` — **PREEXISTENTE**
2. `test_all_eight_uefa_persisted_bindings_are_exact (test_prematch_objective7.Objective7Tests.test_all_eight_uefa_persisted_bindings_are_exact)` — **PREEXISTENTE**
3. `test_atletico_barcelona_is_canonical_and_not_odds_ranked (test_prematch_predictive_evidence_ranking_v1.EvidenceRankingTests.test_atletico_barcelona_is_canonical_and_not_odds_ranked)` — **PREEXISTENTE**
4. `test_canonical_counts_and_all_fixtures (test_prematch_predictive_evidence_ranking_v1.EvidenceRankingTests.test_canonical_counts_and_all_fixtures)` — **PREEXISTENTE**
5. `test_conflicting_published_time_is_rejected (test_prematch_temporal_identity_contract.TemporalIdentityContractTests.test_conflicting_published_time_is_rejected)` — **PREEXISTENTE**
6. `test_discovery_records_excluded_fixtures_individually (test_prematch_window_scope.WindowBoundaryTests.test_discovery_records_excluded_fixtures_individually)` — **PREEXISTENTE**
7. `test_duels_keep_unverified_positional_mechanisms_explicit (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_duels_keep_unverified_positional_mechanisms_explicit)` — **PREEXISTENTE**
8. `test_family_files_and_determinism (test_prematch_predictive_evidence_ranking_v1.EvidenceRankingTests.test_family_files_and_determinism)` — **PREEXISTENTE**
9. `test_frozen_commercial_replay_preserves_no_false_links (test_prematch_objective7.Objective7Tests.test_frozen_commercial_replay_preserves_no_false_links)` — **PREEXISTENTE**
10. `test_halves_use_metric_deltas_not_coverage_only (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_halves_use_metric_deltas_not_coverage_only)` — **PREEXISTENTE**
11. `test_integral_source_hash_mismatch_is_blocked (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_integral_source_hash_mismatch_is_blocked)` — **PREEXISTENTE**
12. `test_json_validation_rejects_tampered_denominator (test_prematch_phase2_player_integrity.PlayerIntegrityTests.test_json_validation_rejects_tampered_denominator)` — **PREEXISTENTE**
13. `test_l5_trace_and_scenarios_do_not_claim_probable_without_xi (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_l5_trace_and_scenarios_do_not_claim_probable_without_xi)` — **PREEXISTENTE**
14. `test_metric_categories_do_not_collapse_on_target_or_xg (test_prematch_predictive_evidence_ranking_v1.EvidenceRankingTests.test_metric_categories_do_not_collapse_on_target_or_xg)` — **PREEXISTENTE**
15. `test_objective6_1_dossiers_are_cutoff_compatible_not_commercial (test_prematch_objective7.Objective7Tests.test_objective6_1_dossiers_are_cutoff_compatible_not_commercial)` — **PREEXISTENTE**
16. `test_offline_event_without_external_reports (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_offline_event_without_external_reports)` — **PREEXISTENTE**
17. `test_offline_integration_sevilla_alaves (test_prematch_integral_analysis_v1046.IntegralAnalysisTests.test_offline_integration_sevilla_alaves)` — **PREEXISTENTE**
18. `test_player_denominators_are_explicit_and_unverifiable_per90_is_suppressed (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_player_denominators_are_explicit_and_unverifiable_per90_is_suppressed)` — **PREEXISTENTE**
19. `test_prematch_v2_phase2 (unittest.loader._FailedTest.test_prematch_v2_phase2)` — **PREEXISTENTE**
20. `test_real_bwin_catalog_link_not_invented (test_prematch_window_scope.HistoricalEvidenceTests.test_real_bwin_catalog_link_not_invented)` — **PREEXISTENTE**
21. `test_real_cache_pilot_is_idempotent_offline_and_preserves_team_patterns (test_prematch_phase2_player_integrity.PlayerIntegrityTests.test_real_cache_pilot_is_idempotent_offline_and_preserves_team_patterns)` — **PREEXISTENTE**
22. `test_real_complete_event_specific_evidence_is_verified (test_prematch_temporal_identity_contract.TemporalIdentityContractTests.test_real_complete_event_specific_evidence_is_verified)` — **PREEXISTENTE**
23. `test_real_enriched_cache_accumulates_and_replays_identically (test_prematch_longitudinal.LongitudinalTests.test_real_enriched_cache_accumulates_and_replays_identically)` — **PREEXISTENTE**
24. `test_real_finish_context_delta_odds_refresh_and_final_review (test_prematch_final_cycle.FinalCycleTests.test_real_finish_context_delta_odds_refresh_and_final_review)` — **PREEXISTENTE**
25. `test_real_goals_regressions_and_direction_separation (test_prematch_coverage_models.RealAuditTests.test_real_goals_regressions_and_direction_separation)` — **PREEXISTENTE**
26. `test_real_offline_family_audit_and_juventus_both_directions (test_prematch_coverage_models.RealAuditTests.test_real_offline_family_audit_and_juventus_both_directions)` — **PREEXISTENTE**
27. `test_real_osасuna_artifacts_are_auditable (test_prematch_market_mapping_v1.MarketMappingTests.test_real_osасuna_artifacts_are_auditable)` — **PREEXISTENTE**
28. `test_report_adapter_deduplicates_and_excludes_post_cutoff (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_report_adapter_deduplicates_and_excludes_post_cutoff)` — **PREEXISTENTE**
29. `test_report_identity_mismatch_does_not_block_statistical_build (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_report_identity_mismatch_does_not_block_statistical_build)` — **PREEXISTENTE**
30. `test_report_inbox_and_previous_pin_remain_intact (test_prematch_final_cycle.FinalCycleTests.test_report_inbox_and_previous_pin_remain_intact)` — **PREEXISTENTE**
31. `test_research_questions_and_priority_components_are_present (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_research_questions_and_priority_components_are_present)` — **PREEXISTENTE**
32. `test_same_report_copied_to_two_files_counts_once (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_same_report_copied_to_two_files_counts_once)` — **PREEXISTENTE**
33. `test_sos_hash_is_authentic_in_offline_run (test_prematch_integral_analysis_v1046.IntegralAnalysisTests.test_sos_hash_is_authentic_in_offline_run)` — **PREEXISTENTE**
34. `test_source_cutoff_is_preserved (test_prematch_tactical_intelligence_v1050.TacticalIntelligenceTests.test_source_cutoff_is_preserved)` — **PREEXISTENTE**
35. `test_stale_capture_is_rejected (test_prematch_temporal_identity_contract.TemporalIdentityContractTests.test_stale_capture_is_rejected)` — **PREEXISTENTE**
36. `test_unknown_timezone_is_rejected (test_prematch_temporal_identity_contract.TemporalIdentityContractTests.test_unknown_timezone_is_rejected)` — **PREEXISTENTE**
37. `test_wrong_event_contract_is_rejected (test_prematch_temporal_identity_contract.TemporalIdentityContractTests.test_wrong_event_contract_is_rejected)` — **PREEXISTENTE**

## 4. Clasificación de causas heredadas

- **Errores de ruta tras migración de tests a `test/`**: varios tests (`test_prematch_reuse_odds_contract`, `test_prematch_temporal_identity_contract`, `test_prematch_comparison_v21`, `test_prematch_bwin_phase3`, etc.) resuelven rutas relativas a `__file__.parent` (`test/`) en vez de la raíz del repositorio.
- **Incompatibilidades previas de dominio/contrato**: `test_filter_adult_competitions`, `test_p0_repair`, `test_prematch_functional_isolation`, `test_discovery_fixture_audit_fixes`, etc., ya fallaban en `task-12` antes de la Iteración 1.
- Ninguno de estos 54 casos tiene relación con P0-A o P0-B y no se tocaron en esta iteración.
