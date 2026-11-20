# Auditoría de viabilidad: reducir el mínimo de `prepare` de 45 a 30 minutos

**Decisión:** **MANTENER `prepare` en 45 minutos (`PREPARE_MIN_LEAD_MINUTES = 45`)** sin modificarlo en esta corrección puntual.

## 1. Dónde se exige actualmente `45` minutos para `prepare`

- `prematch_single.py`: `PREPARE_MIN_LEAD_MINUTES = 45` (línea 36); CLI `check`/`prepare` bloquea `--min-lead-minutes < 45` (líneas 1029-1032).
- `prematch_multi.py`: `prepare_one()` fija `freshness(before, min_lead=45)` antes de adquirir cuotas/contexto (línea 275) y `freshness(ctx, min_lead=45)` tras la verificación previa de reutilización (línea 302).
- `prematch_daily.py`: `args.min_lead` por defecto es `45` (línea 956) y las rutas operativas de ranking/preparación exigen `args.min_lead < 45` → `PREPARATION_MINIMUM_45_MINUTES` (líneas 1181, 1185).
- `prematch_prospective_trial.py`: CLI bloquea `--min-lead < 45` (líneas 800-801).
- `prematch_case.py`: `promote_research` usa `45` en `selector.prepare_rows(..., 45, ...)` y `prematch_single.freshness(ctx, min_lead=45)` (líneas 222, 239); `resolve-lineups` usa default `--min-lead-minutes 45` (línea 2086).
- `prematch_runtime.py`: `write_handoff()` publica `operation_windows['preparation']['minimum_lead_minutes'] = 45` mientras separa `manual_bwin_bind_verify_scrape`, `review`, `refresh_odds`, `retry_context` y `finish` en `5` minutos (T-5).
- Tests vigentes que exigen explícitamente `45` para `prepare`:
  - `test/test_prematch_temporal_windows.py` (`single.ensure_prematch(ctx, min_lead=45)` y `operation_windows['preparation']['minimum_lead_minutes'] == 45`).
  - `test/test_prematch_flexible_selection.py` (`test_single_cli_preserves_operation_safety_floors`: `--min-lead-minutes 44` devuelve `SystemExit(2)`).

## 2. Tiempos y dependencias reales dentro de `prepare`

1. **Scraper Bwin (`scraper_bwin.py` + `bwin_market_preflight.py`)**:
   - `MAX_INTENTOS_SCRAPER = 3`, `ESPERA_ENTRE_INTENTOS_SEGUNDOS = 3`, timeout de navegación `60000 ms`, espera inicial de acordeón `45000 ms`, `WAIT_BEFORE_EXTRACTION_MS = 1500 ms` (`config/constants.py`, `scraper_bwin.py`), y `capture_timeout_seconds = 240` por partido en `bwin_market_preflight.py`.
   - En preparación de **un único partido ya identificado** (`prematch_single.py prepare` / `prepare_one`), el scraping completo + generación de contexto (`generate_context.py`, `REQUEST_TIMEOUT = 25`, `MAX_RETRIES = 3`) suele caber técnicamente dentro de 30 minutos.
2. **Barrido diario (`prematch_daily.py`)**:
   - En el pipeline diario completo (`discover_fixtures` + `preselect_matches` con `history = 20`, `details = 8` + `bwin_discovery` + múltiples capturas `--bwin-top 5`), la latencia acumulada más reintentos reduce sensiblemente el margen útil antes del inicio.
3. **Dependencias aguas abajo antes del kickoff (`T-15` y `T-5`)**:
   - La regla de cierre operativo de alineaciones confirmadas (`22 titulares`) corta en **T-15** (`lineup_cutoff_minutes = 15`, `cmd_acquire_lineups` y `resolve_lineups_or_fallback`).
   - Tras `prepare`, aún debe completarse: informe cualitativo, validación, motor probabilístico/Shadow (`finish`), posible captura manual de XI en `lineups/manual/` o cambio al siguiente candidato del ranking, y comprobación final antes de **T-5**.
   - Iniciar todo el ciclo completo `prepare` en **T-30** dejaría como máximo **15 minutos** antes del corte T-15 de alineaciones y **25 minutos** antes del límite operativo T-5.

## 3. Conclusión técnica

- **No es recomendable reducir globalmente `prepare` a 30 minutos** en el estado actual: rompería contratos y tests P0/regresión explícitos (`45` min en 5 módulos y 2 suites) y comprimiría el margen operativo para barrido diario, informe cualitativo y corte de XI (`T-15`).
- Con la corrección aplicada en esta iteración, **ya quedan desacopladas las operaciones tardías hasta T-5**: `--bind-bwin-url --verify-manual-bwin --scrape-manual-bwin`, `refresh-odds`, `acquire-lineups`, `review` y `finish` operan hasta **T-5**, sin alterar ni debilitar la puerta de `prepare` (`45` min).
- Si en el futuro se quisiera permitir `prepare` en `30` minutos **solo** para el caso aislado de evento ya descubierto/enlazado (`prematch_single.py prepare` con `--reuse-odds`), requeriría una decisión de producto específica actualizando `PREPARE_MIN_LEAD_MINUTES`, `prematch_multi.py` (líneas 275 y 302) y las pruebas contractuales que hoy fijan `45`.
