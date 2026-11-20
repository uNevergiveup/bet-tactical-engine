# MATRIZ DE PRUEBAS — CERTIFICACIÓN INTEGRAL PREMATCH

| # | Requisito / Caso Obligatorio | Prueba / Módulo | Resultado | Evidencia |
|---|---|---|---|---|
| 1 | Preparación correcta de expediente único | `test_post_chelsea_psg_p0_fixes.py::test_03`, `test_08` | **OK** | `manifest.json` (`PREPARADO`), SHA de inputs verificados |
| 2 | Selección explícita del fixture | `test_post_chelsea_psg_p0_fixes.py::test_03`, `test_select_single_match.py` | **OK** | `selected_match.json` + `selection_decision.json` validados |
| 3 | XI completos confirmados (22 titulares) | `test_post_chelsea_psg_p0_fixes.py::test_03`, `test_08` | **OK** | `confirmed_complete_22=True`, `eligible_for_actionable_pick=True` |
| 4 | XI incompletos (bloqueo por defecto / modo investigación no accionable) | `test_post_chelsea_psg_p0_fixes.py::test_02`, `test_06` | **OK** | `LINEUPS_NOT_CONFIRMED_22` en precheck sin llamar al scraper |
| 5 | Informe cualitativo correcto | `test_post_chelsea_psg_p0_fixes.py::test_08`, `test_match_identity_v1.py` | **OK** | `APTO_VALIDACION_BASICA`, `blocks=0`, `reviews=0` |
| 6 | Informe cualitativo ausente | `test_post_chelsea_psg_p0_fixes.py::test_02` | **OK** | `REPORT_MISSING` reportado en `pending_requirements` antes del scraper |
| 7 | Informe cualitativo de otro evento | `test_post_chelsea_psg_p0_fixes.py::test_04` | **OK** | `REPORT_EVENT_ID_CONFLICT` / `REPORT_FIXTURE_ID_CONFLICT` bloquea en precheck |
| 8 | Cuotas frescas (`<= 15` min) | `test_post_chelsea_psg_p0_fixes.py::test_03`, `test_08` | **OK** | `odds_age_minutes=0.0`, aceptadas por `finish` y motor |
| 9 | Cuotas caducadas (`> 15` min) | `test_post_chelsea_psg_p0_fixes.py::test_03` | **OK** | `finish` aislado bloquea (`20m > 15m`); `close` refresca y completa |
| 10 | Refresco de cuotas fallido | `test_post_chelsea_psg_p0_fixes.py::test_03` | **OK** | `BLOCKED_REFRESH_ODDS`, `finish` no se ejecuta, sin prompt Codex |
| 11 | URL Bwin incorrecta / formato Markdown | `test_post_chelsea_psg_p0_fixes.py::test_04`, `test_prematch_window_boundaries.py` | **OK** | Rechazo explícito por contradicción de Event ID o formato |
| 12 | Identidad API incompatible | `test_post_chelsea_psg_p0_fixes.py::test_04`, `test_match_identity_v1.py` | **OK** | `IDENTITY_VALIDATION_FAILED` / `MANIFEST_INVALID_OR_MISSING` |
| 13 | SHA alterado en artefactos del expediente | `test_post_chelsea_psg_p0_fixes.py::test_04`, `test_prematch_case.py` | **OK** | Bloqueo cerrado ante discrepancia SHA-256 |
| 14 | Falta de modelo compatible (`MODEL_UNAVAILABLE`) | `test_uwcl_market_audit_regression.py::test_5` | **OK** | Clasificado `MODEL_UNAVAILABLE`, nunca confundido con `NO_BET` |
| 15 | Candidato con evaluación válida | `test_etapa4_probabilistic_and_markets.py` | **OK** | `MODEL_EVALUATED_VALUE_CANDIDATE` con probabilidad y EV auditables |
| 16 | NO BET estadístico genuino | `test_uwcl_market_audit_regression.py`, `test_prematch_final_cycle.py` | **OK** | `MODEL_EVALUATED_NO_VALUE` / `NO_BET` tras evaluación real sin valor |
| 17 | Bloqueo operativo sin falso NO BET | `test_uwcl_market_audit_regression.py::test_5`, `test_6` | **OK** | `INSUFFICIENT_STATISTICAL_EVIDENCE` / `NO_MARKET_CAPTURED` preservados |
| 18 | Margen temporal inferior a `T-5` | `test_post_chelsea_psg_p0_fixes.py::test_05`, `test_prematch_window_boundaries.py` | **OK** | `INSUFFICIENT_PREMATCH_MARGIN` a `T-4m` |
| 19 | Inicio válido y finalización posterior al kickoff | `test_post_chelsea_psg_p0_fixes.py::test_05` | **OK** | Código `3`, revoca `CODEX_<E>.md`, `status=EXPIRADO_POST_KICKOFF` |
| 20 | Kickoff ya iniciado | `test_post_chelsea_psg_p0_fixes.py::test_05`, `test_prematch_final_cycle.py` | **OK** | `POST_KICKOFF` bloquea antes de scraper/validador/motor |
| 21 | Dos ejecuciones consecutivas del mismo expediente | `test_post_chelsea_psg_p0_fixes.py::test_03` | **OK** | `FINAL_PREMATCH_CLOSURE_ALREADY_VALID` impide sobrescritura |
| 22 | Reintento tras fallo parcial | `test_post_chelsea_psg_p0_fixes.py::test_03` | **OK** | Recuperación limpia tras `BLOCKED_REFRESH_ODDS` |
| 23 | Sincronización manual de XI sin `case.json` | `test_post_chelsea_psg_p0_fixes.py::test_06`, `test_manual_lineup_inbox_flow.py` | **OK** | `sync_manual_lineups` y auto-reconciliación en `close` operativos |
| 24 | Reejecución de Shadow tras cambios de XI y cuotas | `test_post_chelsea_psg_p0_fixes.py::test_08`, `test_p0_temporal_and_shadow_sync.py` | **OK** | `shadow_summary_7913441.json` sincronizado con el nuevo `odds_sha256` |
| 25 | Persistencia correcta y aislamiento entre eventos | `test_post_chelsea_psg_p0_fixes.py::test_04`, `test_08` | **OK** | Expedientes `7913441` y `7901472` aislados sin contaminación cruzada |
| E2E | Ciclo E2E real con subprocesos reales (`close --shadow-mode`) | `test_post_chelsea_psg_p0_fixes.py::test_08` | **OK** | `audit/reparacion_integral_prematch/E2E_PREMATCH_RESULT.json` |
