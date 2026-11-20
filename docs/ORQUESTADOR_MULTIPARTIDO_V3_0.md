# BET ENGINE V3.0 — Orquestador Multipartido con Top 3 Dinámico por Franjas

## A. Arquitectura del Orquestador V3.0 y relación con los módulos existentes

V3.0 introduce una capa de planificación y ejecución secuencial reanudable por franjas horarias que **reutiliza íntegramente** las piezas nucleares de Bet Engine sin duplicar pipelines paralelos ni romper el flujo de partido único:

- **Contratos y máquina de estados V3.0**: [`bet_engine/contracts/multi_slot.py`](../bet_engine/contracts/multi_slot.py)
  - Define las franjas semiabiertas `[inicio, fin)` en `Europe/Madrid` con cruce de medianoche y normalización DST (`TimeSlotSpec`, `build_window_slots`, `parse_custom_slots_spec`).
  - Define la máquina explícita de 14 estados (`MultiMatchExecutionState`) y las reglas de transición (`ALLOWED_STATE_TRANSITIONS`), separando estados activos (`ACTIVE_SLOT_STATES`), terminales (`TERMINAL_EXECUTION_STATES`) y expedientes cerrados inmutables (`CLOSED_DOSSIER_STATES`).
  - Mantiene invariantes estrictos (`WindowOrchestrationPlan.validate_invariants()`): máximo 1 partido activo por franja, máximo 3 partidos activos en la ventana, y unicidad absoluta de `fixture_id` y `event_id`.
- **Ranking global, por franja y reordenación dinámica**: [`bet_engine/orchestration/multi_ranker.py`](../bet_engine/orchestration/multi_ranker.py)
  - Reutiliza `prematch_tactical_signal_engine.analyze_fixture` (cuando existen observaciones estadísticas) junto a `data_readiness_score` y `research_priority_score` observados en `preselection.json` sin fabricar métricas ausentes.
  - Calcula elegibilidad PREMATCH separada de la calidad y de la decisión probabilística (`populate_and_rank_plan`, `apply_dynamic_ranking_update`, `select_best_eligible_for_slot`), registrando cada variación en `ranking_history` y congelando expedientes ya cerrados.
- **Orquestador secuencial por franjas**: [`bet_engine/orchestration/multi_orchestrator.py`](../bet_engine/orchestration/multi_orchestrator.py)
  - Coordina resolución/encadenamiento de `preselection.json` (`resolve_or_generate_preselection`), preparación aislada en `outputs/matches/<EVENT_ID>/` (`default_case_preparer` + `prepare_manual_lineup_inbox`), espera opcional cualitativa (`WAITING_RESEARCH`), control P0 de XI (`evaluate_fixture_lineup_state`, `validate_manual_lineup_payload`, `scan_manual_lineup_inbox`), fallback acotado en la misma franja y evaluación Shadow independiente (`run_shadow_for_event_dir`).
- **CLI único unificado**: [`prematch_orchestrator.py`](../prematch_orchestrator.py)
  - Subcomandos: `plan`, `top3`, `status`, `step`, `run`, `resume`, `attach-lineups`, `attach-research`, `summary`.
  - Protege por defecto toda ejecución sin llamadas externas (`--offline` implícito) y bloquea `--online` sin `--confirm-external` salvo en `--dry-run` (`SAFETY_BLOCK`).
- **Relación con scripts existentes**:
  - `prematch_daily.py`, `prematch_case.py`, `prematch_single.py` y `prematch_multi.py` conservan su compatibilidad e invariantes actuales.
  - `prematch_multi.py` continúa como ejecutor de lotes URL bajo demanda; V3.0 aporta el planificador diario stateful por franjas horarias que faltaba.

---

## B. Causa raíz del error previo (`preselection.json` faltante en `--rank`) y solución V3.0

Al auditar [`prematch_daily.py`](../prematch_daily.py) y [`prematch_prospective_trial.py`](../prematch_prospective_trial.py) se verificó exactamente por qué fallaba `prematch_daily.py --rank --online --select-verified`:

1. En `prematch_daily.py` (`operational_research`, líneas ~358-371), la rama `--rank` asume que `preselection.json` ya existe y apunta directamente a:
   `trial.latest_evaluation(window_folder) / 'prospective_trial/enriched/preselection.json'`.
2. Sin embargo, `trial.prepare(root, folder, day, args)` —que ejecuta `discover_fixtures.py`, `filter_major_leagues.py`, `preselect_matches.py`, `prematch_pattern_discovery.py`, `prematch_pattern_engine.py` y `bwin_discovery.py`— solo se invoca en la rama por defecto de `main()` (línea ~1126), después del retorno anticipado de `--rank` / `--select-verified`.
3. En consecuencia, ejecutar `--rank` sobre una ventana nueva (sin una ejecución previa completa de `prematch_daily.py`) fallaba de inmediato por ausencia de `preselection.json`.

**Solución implementada en V3.0 (`resolve_or_generate_preselection`)**:
- Busca primero `outputs/discovery/<YYYY-MM-DD_HHMM_HHMM>/evaluations/*/prospective_trial/enriched/preselection.json` (o ruta explícita `--preselection`) **exclusivamente** de esa ventana. Nunca recicla otra fecha.
- Si no existe:
  - En modo offline con proveedor inyectado (`preselection_provider`), encadena y valida el artefacto antes de rankear (`preselection_auto_generated=True`).
  - En modo `--online --confirm-external`, invoca `prematch_prospective_trial.prepare(...)` para construir discovery, filtros y enriquecimiento de esa misma ventana.
  - En modo `--dry-run`, reporta `plan_path` y estructura vacía simulada sin escribir ni consultar APIs.

---

## C. Modelo de datos, franjas horarias y máquina de estados

### C.1 Franjas por defecto en `Europe/Madrid` (configurables vía `--slots`)
Para la ventana operativa estándar `15:00 -> 03:00` (con cruce de medianoche al día siguiente e intervalos semiabiertos `[inicio, fin)`):
- `FRANJA_A`: `[15:00, 18:00)` — máximo 1 partido activo.
- `FRANJA_B`: `[18:00, 21:00)` — máximo 1 partido activo.
- `FRANJA_C`: `[21:00, 03:00)` del día siguiente — máximo 1 partido activo.

Las conversiones pasan explícitamente por `Europe/Madrid` -> UTC (`ZoneInfo("Europe/Madrid")`), gestionando correctamente cambios CET/CEST (23h o 25h diarias; verificado en test DST de 13h reales en ventana nocturna del cambio de octubre).

### C.2 Los 14 estados explícitos (`MultiMatchExecutionState`)
- Flujo activo/progresivo: `DISCOVERED`, `PRESELECTED`, `RANKED`, `SELECTED`, `PREPARING`, `WAITING_RESEARCH`, `WAITING_LINEUPS`, `READY_FOR_FINALIZATION`.
- Estados terminales/cerrados: `FINALIZED`, `NO_BET`, `SKIPPED_INELIGIBLE`, `REJECTED_MISSING_LINEUPS`, `FAILED_TECHNICAL`, `EXPIRED`.

### C.3 Separación estricta de dimensiones en `SlotCandidateRecord`
Cada candidato persiste los campos obligatorios separados sin mezclar ejecución con probabilidad ni auditoría:
`slot_id`, `fixture_id`, `event_id`, `kickoff_utc`, `kickoff_madrid`, `league_name`, `global_rank`, `slot_rank`, `quality_score`, `eligibility_status`, `execution_state`, `lineup_state`, `odds_state`, `research_state`, `shadow_decision` (nullable), `fallback_parent_fixture_id` (nullable), `last_updated_utc`, además de `audit_outcome`, `blocking_reasons`, `quality_components`, `fallback_chain_depth`, `attempts` y `candidate_selection`.

---

## D. Algoritmo de ranking global, ranking por franja, reordenación dinámica y fallback

1. **Ranking global (`global_rank`)**: ordena todos los candidatos de la ventana por `(-elegible, -quality_score, kickoff_utc, fixture_id)`.
2. **Ranking por franja (`slot_rank`)**: ordena dentro de cada `slot_id` de forma independiente.
3. **Promoción inicial**: únicamente el candidato `RANKED` con mejor `slot_rank` y `eligibility_status == "ELIGIBLE"` pasa a `SELECTED` por franja (máximo 1 por franja, 3 globales). Si una franja no tiene candidatos elegibles o ninguno supera los umbrales de calidad/datos, queda `EMPTY`.
4. **Reordenación dinámica (`apply_dynamic_ranking_update`)**: recalcula elegibilidad temporal y de mercado en cada `step`, registra deltas con timestamp en `ranking_history`, y **prohíbe mutar** expedientes ya en `CLOSED_DOSSIER_STATES` (`FINALIZED`, `NO_BET`, `REJECTED_MISSING_LINEUPS`, `FAILED_TECHNICAL`, `EXPIRED`).
5. **Fallback acotado dentro de la misma franja**: únicamente cuando un candidato falla antes del cierre analítico (`REJECTED_MISSING_LINEUPS`, `FAILED_TECHNICAL`, `SKIPPED_INELIGIBLE`, `EXPIRED`) y `budget.can_fallback_slot(slot_id)` lo permite, se evalúa el siguiente elegible de esa misma franja si aún cumple `min_lead_minutes` (45 min por defecto). Un `NO_BET` analítico (`NO_BET_POR_PRECIO` / `NO_BET_POR_EVIDENCIA`) **cierra la franja** y nunca fuerza reemplazos artificiales.

---

## E. Integración con P0 (XI confirmados), inbox manual y Shadow Mode

- Cada partido promovido inicializa su bandeja aislada `outputs/matches/<EVENT_ID>/lineups/manual/` mediante `prepare_manual_lineup_inbox`.
- `MultiSlotOrchestrator` evalúa API-Football, JSON manual (`validate_manual_lineup_payload(..., allow_probable_only=True)`) e informes cualitativos a través de `evaluate_fixture_lineup_state`.
- **Bloqueo duro P0**: `LINEUPS_PROBABLE_ONLY`, `LINEUPS_PENDING` o 1 solo equipo confirmado nunca pasan a `FINALIZED` ni pueden producir `PICK_ACCIONABLE` (incluso si Shadow devolviera señal positiva, `_finalize_candidate_with_shadow` degrada explícitamente a `CANDIDATO_CONDICIONAL`).
- Al corte (`T-15m` configurable con `--lineup-cutoff`), la ausencia de 22 titulares confirmados transiciona a `REJECTED_MISSING_LINEUPS` y dispara fallback en esa franja si existe reemplazo elegible con tiempo suficiente.
- Con 22 XI confirmados (vía API, externa o `attach-lineups` / inbox manual antes del corte), se ejecuta `run_shadow_for_event_dir` de manera independiente sobre `outputs/matches/<EVENT_ID>/` y se clasifica exactamente con la taxonomía de 6 categorías: `PICK_ACCIONABLE`, `CANDIDATO_CONDICIONAL`, `NO_BET_POR_PRECIO`, `NO_BET_POR_EVIDENCIA`, `BLOQUEO_TECNICO`, `MERCADO_NO_MODELADO`.

---

## F. Pruebas realizadas y matriz de escenarios E2E (A–M)

Suite ejecutada: `test/test_multi_slot_contracts_and_ranker.py` (3 tests) + `test/test_multi_slot_orchestrator_v3.py` (13 tests, cubriendo los 13 escenarios obligatorios + seguridad CLI) = **16 tests, 16 OK**.

| Escenario / Prueba | Resultado | Comportamiento verificado |
|---|---|---|
| Contratos y fronteras DST (`test_midnight_and_dst_slot_boundaries`) | OK | Intervalos `[inicio, fin)`, cruce `23:59 -> 00:00 -> 03:00` y noche DST 13h |
| Invariantes y estados (`test_state_machine_and_duplicate_invariants`) | OK | Rechazo de `fixture_id` duplicado y transiciones prohibidas tras `FINALIZED` |
| Reordenación dinámica (`test_global_slot_ranking_and_dynamic_rerank_preserves_closed_dossiers`) | OK | `ranking_history` auditado y preservación de dossier cerrado |
| **A** — 3 franjas válidas | OK | 3 partidos seleccionados (1/franja), `FINALIZED` / `NO_BET` independientes |
| **B** — 2 franjas elegibles | OK | Franja A y B finalizan; Franja C queda `EMPTY` sin forzar picks |
| **C** — Falta de XI al corte + fallback | OK | Franja A recupera con 2º elegible (`>=45m`); Franja B sin margen cierra vacía |
| **D** — Recuperación manual XI + Shadow | OK | `attach_manual_lineup_file` valida JSON, pasa a `READY_FOR_FINALIZATION` y cierra `FINALIZED` |
| **E** — XI probables (`LINEUPS_PROBABLE_ONLY`) | OK | Bloqueo duro: jamás promueve a `PICK_ACCIONABLE` ni `FINALIZED` |
| **F** — Sin `preselection.json` previo | OK | Resuelve dependencia offline (`preselection_auto_generated=True`) y completa ciclo |
| **G** — Cruce de medianoche CET | OK | `23:55 CET`, `00:30 CET`, `02:59:59 CET` en `FRANJA_C`; `03:00:00 CET` excluido |
| **H** — Interrupción en `WAITING_RESEARCH` y `resume` | OK | Pausa limpia, adjunta informe, reanuda sin duplicar preparación ni Shadow |
| **I** — Fallo de proveedor aislado | OK | Error en partido de Franja A queda `FAILED_TECHNICAL`; B y C continúan normal |
| **J** — 30 partidos de baja calidad | OK | Los 30 pasan a `SKIPPED_INELIGIBLE` con motivos; las 3 franjas terminan `EMPTY` |
| **K** — Aislamiento de snapshots y análisis | OK | Marcadores y artefactos separados por `outputs/matches/<EVENT_ID>/` |
| **L** — Idempotencia en `resume` repetido | OK | Sin duplicados ni reejecuciones sobre expedientes cerrados (`attempts == 1`) |
| **M** — Topes de presupuesto y anti-bucle | OK | Respeta `max_slot_fallbacks=1` y detiene fallbacks en bucle |
| **CLI** — Subcomandos y bloqueo `--online` | OK | `prematch_orchestrator.py plan` OK; `--online` sin `--confirm-external` retorna código `2` (`SAFETY_BLOCK`) |

---

## G. Cambios realizados archivo por archivo

| Archivo | Acción | Propósito |
|---|---|---|
| `bet_engine/contracts/multi_slot.py` | Nuevo | Franjas `[start, end)`, 14 estados, `SlotCandidateRecord`, `WindowOrchestrationPlan`, `ResourceBudgetLedger` |
| `bet_engine/contracts/__init__.py` | Editado | Exportación pública de contratos V3.0 |
| `bet_engine/orchestration/multi_ranker.py` | Nuevo | Ranking global/por franja, calidad sin fabricación, elegibilidad separada y reordenación dinámica |
| `bet_engine/orchestration/multi_orchestrator.py` | Nuevo | Orquestador secuencial reanudable, resolución de `preselection.json`, P0 XI, fallback por franja y reportes |
| `bet_engine/orchestration/__init__.py` | Editado | Exportación de `MultiSlotOrchestrator`, `OrchestratorConfig`, `OrchestratorAdapters` y funciones de ranking |
| `prematch_orchestrator.py` | Nuevo | CLI unificado (`plan`, `top3`, `status`, `step`, `run`, `resume`, `attach-lineups`, `attach-research`, `summary`) |
| `test/test_multi_slot_contracts_and_ranker.py` | Nuevo | Pruebas unitarias de Etapa 1 y Etapa 2 |
| `test/test_multi_slot_orchestrator_v3.py` | Nuevo | Pruebas E2E offline de los 13 escenarios (A–M) y seguridad CLI |
| `docs/ORQUESTADOR_MULTIPARTIDO_V3_0.md` | Nuevo | Informe técnico completo V3.0 |

*(No se modificaron snapshots congelados históricos como `outputs/matches/7909704/*` ni `outputs/matches/7912816/*`.)*

---

## H. Resultados de regresión de toda la suite de tests

1. **Suites activas V2 + P0 + Auditorías + V3.0 (24 suites)**:
   - Comando: `PYTHONPATH=test:. .venv/bin/python -m unittest test_multi_slot_contracts_and_ranker test_multi_slot_orchestrator_v3 test_manual_lineup_inbox_flow test_lineup_fallback_e2e test_lineup_management_p0 test_corrective_audit_v10 test_etapa2_contracts_and_store test_etapa3_analytics_core test_etapa4_probabilistic_and_markets test_etapa5_shadow_and_lifecycle test_etapa5_2_cli_shadow_integration test_market_coverage_and_taxonomy_stage1 test_probabilistic_audit_stage2 test_prematch_case test_prematch_case_resume_closure test_bwin_discovery_v31 test_discovery_policy_blocks test_discovery_fixture_audit_fixes test_prematch_tactical_signal_engine test_prematch_comparison_v21 test_prematch_research_profiles test_prematch_flexible_selection test_prematch_daily test_select_single_match -v`
   - Resultado exacto: **`Ran 201 tests in 14.346s — OK (skipped=1)`** (`test_frozen_pasto_junior_identity_remains_exact`, `'frozen reconciliation artifact unavailable'`). **0 fallos, 0 errores, 0 regresiones nuevas.**

2. **Barrido completo del directorio `test/` (`unittest discover`)**:
   - Comando: `PYTHONPATH=test:. .venv/bin/python -m unittest discover -s test`
   - Resultado exacto: **`Ran 943 tests in 57.096s — FAILED (failures=16, errors=37, skipped=17)`**.
   - Clasificación verificada: los 16 fallos y 37 errores pertenecen íntegramente a suites heredadas desfasadas frente a contratos actuales (p. ej. `test_filter_adult_competitions.py`, `test_p0_repair.py`, `test_prematch_functional_isolation.py`, `test_prematch_reuse_odds_contract.py`), documentadas ya en auditorías previas; **ninguno** pertenece a las 24 suites activas ni a los módulos V3.0 creados.

3. **Verificación CLI (`--help` y `--dry-run` sin llamadas externas)**:
   - Verificados `--help` de `prematch_orchestrator.py` (principal + `attach-lineups` + `attach-research`), `prematch_daily.py`, `prematch_case.py`, `prematch_single.py`, `prematch_multi.py`.
   - Verificados `--dry-run` de `plan`, `status` y `run` sobre la ventana `2026-11-17 15:00 -> 2026-11-18 03:00` CET sin mutar disco ni ejecutar red.

---

## I. Riesgos, limitaciones y puntos pendientes antes de ejecución online real

1. **Integración online end-to-end no ejecutada en vivo**: por mandato estricto de seguridad, todas las pruebas se ejecutaron offline con stubs/snapshots controlados; el encadenamiento vivo `--online --confirm-external` (`trial.prepare` + `bwin_discovery.py` + API-Football real) queda pendiente de primera prueba controlada en jornada real.
2. **Investigación cualitativa y lectura visual de capturas XI**: V3.0 pausa limpiamente en `WAITING_RESEARCH` o `WAITING_LINEUPS` y provee la bandeja `outputs/matches/<EVENT_ID>/lineups/manual/`, pero no invoca automáticamente navegadores ni LLMs externos; requiere adjuntar el `.md` de Grok/Perplexity o el JSON validado de XI mediante `attach-research` / `attach-lineups`.
3. **Suites heredadas en `test/`**: existen 53 discrepancias (16 failures + 37 errors) en pruebas históricas no mantenidas fuera del núcleo activo; conviene archivarlas o actualizarlas aparte para evitar ruido en `unittest discover`.

---

## J. Comandos exactos recomendados para operación real (NO ejecutados online)

Ejemplo para la ventana operativa `2026-11-17 15:00` → `2026-11-18 03:00 Europe/Madrid`:

```bash
# 1. Simular estructura de franjas sin mutar archivos ni red
.venv/bin/python prematch_orchestrator.py plan --date 2026-11-17 --start 15:00 --end 03:00 --dry-run

# 2. Planificar ventana real (genera o reutiliza preselection.json de la ventana exacta)
.venv/bin/python prematch_orchestrator.py plan --date 2026-11-17 --start 15:00 --end 03:00 --online --confirm-external

# 3. Consultar Top 3 global y líder elegible por franja
.venv/bin/python prematch_orchestrator.py top3 --date 2026-11-17

# 4. Avanzar ciclo secuencial pausando si falta investigación cualitativa o XI confirmados
.venv/bin/python prematch_orchestrator.py run --date 2026-11-17 --require-research --offline

# 5. Adjuntar informe cualitativo cuando esté listo para un evento
.venv/bin/python prematch_orchestrator.py attach-research --date 2026-11-17 --event-id <EVENT_ID> --source outputs/matches/<EVENT_ID>/inputs/research.md

# 6. Incorporar XI manuales verificados (cuando API-Football no confirme 22 titulares antes de T-15m)
.venv/bin/python prematch_orchestrator.py attach-lineups --date 2026-11-17 --event-id <EVENT_ID> --source outputs/matches/<EVENT_ID>/lineups/manual/manual_lineup_<EVENT_ID>.json

# 7. Reanudar ventana de forma idempotente hasta cierre o resumen final
.venv/bin/python prematch_orchestrator.py resume --date 2026-11-17 --offline
.venv/bin/python prematch_orchestrator.py summary --date 2026-11-17
```

---

## K. Valoración honesta y clasificación de madurez operativa

- **Clasificación**: **`READY_FOR_CONTROLLED_PREMATCH_TRIAL`** (con verificación offline completa y `0` regresiones en las 24 suites activas).
- **Motivo de la clasificación**: toda la lógica de franjas con cruce de medianoche/DST, máquina de estados de 14 estados, aislamiento por `EVENT_ID`, control estricto P0 de XI confirmados, fallback acotado por franja, presupuestos de recursos, reanudación idempotente y bloqueo de `--online` sin confirmación están implementados y probados en 16 pruebas dedicadas (escenarios A–M). No se declara producción desatendida (`PRODUCTION_AUTONOMOUS`) porque la primera corrida online supervisada contra proveedores reales debe validar latencias de `trial.prepare` y emparejamiento Bwin en vivo.
