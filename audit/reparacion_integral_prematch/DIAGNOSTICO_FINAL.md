# DIAGNÓSTICO FINAL — REPARACIÓN INTEGRAL Y CERTIFICACIÓN PREMATCH

**Proyecto:** `bet-tactical-engine`  
**Rama:** `refactor/etapa2-fundamentos`  
**Fecha de auditoría:** `2026-11-18`

---

## 1. Resumen Ejecutivo

Tras el ensayo real con **Chelsea W vs Paris Saint Germain W** (API `1638301`, Bwin `7913441`, kickoff `2026-11-18T21:00:00+01:00`), donde las etapas individuales de preparación, adquisición de 22 titulares, actualización diferencial de contexto, refresco de cuotas, evaluación Shadow y validación básica con identidad (`BLOCK=0`, `REVIEW=0`) funcionaron correctamente pero el cierre manual se vio bloqueado por desincronización temporal entre `refresh-odds` (ejecutado a `T-21.9m`) y `finish` (intentado cuando las cuotas tenían `16.7m > 15m` y un segundo refresco a `T-3.1m < 5m`), se auditó y reparó integralmente la cadena de cierre, la interoperabilidad entre módulos y la suite general completa de pruebas.

---

## 2. Causas Raíz, Defectos Identificados y Resoluciones

### D1 (P0.1 / P0.2 / P0.3) — Descoordinación entre `refresh-odds` y `finish` en la ventana crítica `T-15..T-5`
- **Causa raíz:** El operador debía ejecutar `refresh-odds` y posteriormente `finish` como pasos manuales separados. Cualquier revisión intermedia (informe, validación manual, XI) consumía la ventana de frescura de 15 minutos de las cuotas y empujaba un segundo refresco por debajo del margen operativo mínimo de `T-5`. Además, si `finish` concluía justo después del kickoff, `prematch_single.py` emitía una advertencia pero dejaba `CODEX_<EVENT_ID>.md` y `status="FINALIZADO"` activos en disco.
- **Resolución (`prematch_single.py`, `prematch_case.py`, `prematch_multi.py`, `validate_prematch.py`):**
  1. Implementado el subcomando coordinado `prematch_single.py close` (y delegado desde `prematch_case.py close`).
  2. **Precheck integral (`evaluate_close_prerequisites`) antes de tocar el scraper:** verifica margen temporal (`>= 5` min y `< kickoff`), `manifest.json` y hashes SHA-256, `context` + `context_json`, `match_identity.json` (`validate_identity`), auto-reconciliación y confirmación de 22 titulares (`_reconcile_latest_lineups_for_close`) y validez de metadatos/equipos del informe cualitativo. Si falta cualquier requisito, reporta **todos** los bloqueos pendientes en `pending_requirements` y aborta sin invocar el scraper.
  3. **Secuencia atómica `refresh-odds -> verificación -> finish`:** refresca cuotas, verifica nueva captura del mismo evento (`SHA-256`, `age <= 15m`, `remain >= 5m`), ejecuta `finish` inmediatamente y comprueba que tanto `ranking_probabilistico_<E>.json` como `shadow_summary_<E>.json` hayan consumido el `SHA-256` exacto de las cuotas recién capturadas.
  4. **Revocación estricta post-kickoff:** si el reloj alcanza o supera el kickoff al terminar `finish`, archiva y elimina `CODEX_<E>.md` y `handoff_codex_<E>.json`, marca `status="EXPIRADO_POST_KICKOFF"`, `codex_prompt=None` y `eligible_for_actionable_pick=False`, conservando artefactos únicamente para auditoría retrospectiva.
  5. **Reloj inyectable (`--now-utc` / `PREMATCH_NOW_UTC`):** soportado de extremo a extremo sin alterar el reloj de producción.

### D2 (P0.4 / P0.5) — `CANONICAL_CASE_NOT_PREPARED` y sincronización de XI en expedientes de `prematch_single.py`
- **Causa raíz:** `prematch_case.py::_load_case` exigía la existencia física de `case.json`, archivo creado solo por `adopt_selected` pero no por `prematch_single.py prepare` (que persiste `manifest.json`, `single_execution.json` e `inputs/match_identity.json`).
- **Resolución (`prematch_case.py`, `prematch_single.py`):**
  - `_load_case` reconstruye de forma verificada el descriptor `case` a partir de `manifest.json` + `single_execution.json` + `match_identity.json` (comprobando `event_id`, `fixture_id`, `kickoff_madrid`, `competition` y `selected_sha256`), manteniendo el bloqueo `CANONICAL_CASE_NOT_PREPARED` cuando el expediente realmente no ha sido preparado.
  - Resuelta la colisión de flags de XI mediante grupo mutuamente excluyente (`--require-confirmed-lineups` por defecto frente a `--allow-unconfirmed-lineups`, el cual fuerza `eligible_for_actionable_pick=False` y `closure_mode="RESEARCH_ONLY_UNCONFIRMED_LINEUPS"`).

### D3 (P0.6) — Resolución no uniforme de la ruta del informe cualitativo
- **Causa raíz:** `prematch_single.py` y `prematch_multi.py` buscaban en `root/instructions/` y `directory/inputs/`, omitiendo `directory/instructions/reporte_cualitativo_<E>.md`, mientras que `prematch_case.py::_evaluate_closure` solo consultaba `manifest.inputs`.
- **Resolución (`prematch_single.py::resolve_qualitative_report_path`, `prematch_multi.py`, `prematch_case.py`):**
  - Unificada la resolución conservando siempre `--report` explícito y, en su ausencia, buscando determinísticamente en:
    1. `outputs/matches/<E>/instructions/reporte_cualitativo_<E>.md`
    2. `outputs/matches/<E>/inputs/reporte_cualitativo_<E>.md`
    3. `instructions/reporte_cualitativo_<E>.md`

### D4 — Normalización de género (`"F"` frente a `"female"`) en `match_identity.py`
- **Causa raíz:** `match_identity.json` almacena `"category": "F"` desde `home_gender`/`away_gender`, pero `compare_report_name` comparaba literalmente `expected != 'female'`, provocando falso `MISMATCH` en partidos femeninos con `--identity`.
- **Resolución (`match_identity.py`):**
  - Añadido `normalize_category_gender()` aplicado en `compare_report_name()` y `validate_identity()`, preservando el rechazo estricto de conflictos reales entre géneros (`Chelsea Men` en fixture femenino o `Real Madrid Femenino` en fixture masculino).

### D5 — Propagación de `--confirm-external`, refresco sin `--preflight`, estado `quote_capture_status` y aviso `>120m`
- **Resolución (`prematch_daily.py`, `prematch_phase1.py`, `prematch_execution_control.py`, `prematch_runtime.py`, `prematch_daily_csv.py`):**
  - Propagado `--confirm-external` hacia `bwin_discovery.py` en `prematch_daily.py` (`--sync-bwin`), `prematch_phase1.py::run_bwin_once` y `prematch_execution_control.py::discover_bwin`.
  - Habilitado `--refresh-existing-odds` en `prematch_daily.py::select_verified_bwin` cuando `preflight is None` (usando `--immutable-capture` si existe `manifest.json`).
  - Corregida la precedencia de `quote_capture_status` en `prematch_runtime.py` y `prematch_daily_csv.py` para que una captura verificada (`CAPTURED_VERIFIED`) actualice `manual_links.json` y no quede eclipsada por el estado inicial `NOT_CAPTURED`.
  - Diferenciada en `PRESELECTION_OLDER_THAN_120_MINUTES` la línea base histórica reutilizable (`historical_baseline_reusable=True`) frente a las evidencias actuales sujetas a refresco (`schedule/lineups/odds`).

### D6 — Saneamiento de la suite general tras traslado a `test/` (5 fallos y 38 errores -> 0 fallos y 0 errores)
- **Causas raíz identificadas y corregidas:**
  1. Dieciocho módulos de test trasladados a `test/` conservaban `ROOT = Path(__file__).resolve().parent` o `Path('../outputs/...')`, apuntando a `test/outputs/` en vez de la raíz del repositorio.
  2. `prematch_test_fixtures.py` importaba `from test.test_prematch_integral_analysis_v1046` sin fallback a `test_prematch_integral_analysis_v1046` bajo `unittest discover -s test`.
  3. `generate_context.py::JSONCache.get` inyectaba `_acquired_at_utc` incluso en llamadas directas sin `asof` ni `include_acquired`, rompiendo la igualdad exacta del payload de caché en `test_prematch_functional_isolation.py`.
  4. `prematch_runtime.py` no propagaba `--now-utc` a los subprocesos `bwin_market_preflight.py` y `prematch_opportunity_scanner.py` durante replays offline con reloj simulado (`asof`), causando que en días posteriores el subproceso clasificara los snapshots como históricos.
  5. `test_discovery_coverage_ranking_fixes.py::test_07` etiquetaba `"Hoy / %H:%M"` cuando `now_m + 5h` cruzaba la medianoche (`> 19:00 Europe/Madrid`).
