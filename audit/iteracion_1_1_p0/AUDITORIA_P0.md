# AUDITORÍA P0 — Iteración 1.1 (`bet-tactical-engine`)

**Rama:** `refactor/etapa2-fundamentos`  
**HEAD:** `7390981d8da690e34eb96a1dbf944d9e67546708` (`feat: expand frozen international statistical exploration`)  
**Fecha de auditoría:** `2026-11-18`  
**Alcance:** Verificación y endurecimiento de correcciones P0-A (identidad temporal Bwin) y P0-B (sincronización de Shadow tras `refresh-odds`). Sin modificaciones a los expedientes históricos de `outputs/matches/` ni a la calibración probabilística (Iteración 2).

---

## 1. Línea base del repositorio y alcance real de los cambios

El árbol de trabajo contenía ya antes de la Iteración 1 modificaciones y archivos sin seguimiento (`bet_engine/`, `prematch_case.py`, etc.). Los archivos modificados/añadidos específicamente por P0 (Iteración 1 + Iteración 1.1) son únicamente:

1. `generate_context.py` (`COUNTDOWN_RE`, `resolve_browser_event_kickoff`, `browser_event_datetime_utc`, `BwinScraper.parse_url`, `fetch_bwin_html`, `main`)
2. `bwin_market_preflight.py` (`CATALOG_COUNTDOWN_RE`, `time_evidence`)
3. `match_identity.py` (`identity_from_payload`)
4. `validate_prematch.py` (`main` / bloque de auditoría de `match_identity.json`)
5. `bet_engine/orchestration/adapters.py` (`run_shadow_for_event_dir` — archivo untracked previo)
6. `prematch_single.py` (`cmd_refresh`, `cmd_refresh_context`)
7. `prematch_case.py` (`_evaluate_closure`, `refresh_odds_from_snapshot`, `export_handoff` — archivo untracked previo)
8. `prematch_multi.py` (`finish`)
9. `test/test_p0_temporal_and_shadow_sync.py` (nuevo archivo de pruebas P0; 13 tests)

---

## 2. Auditoría P0-A: Identidad temporal Bwin

### 2.1 Caso `7909713` (Argentina–Costa Rica) vs `7909714` (Chile–Colombia)

- **Evidencia `7909713`** ([`outputs/matches/7909713/inputs/bwin_metadata_20261117T220341743119Z.json`](../../outputs/matches/7909713/inputs/bwin_metadata_20261117T220341743119Z.json), SHA-256 `184ed56464b58b3b520e03b50a5ef2786932933b5d7ffb9c2c8e644774bb260e`):
  - `bwin_event_id`: `"7909713"`
  - `text_source`: `"SINGLE_EVENT_TILE"`
  - `browser_timezone`: `"Europe/Madrid"`
  - `text_observed_at_utc`: `"2026-11-17T22:03:41.738633+00:00"`
  - `text`: `"Argentina Costa Rica Comienza en 56 min CA 1 1.07 X 13.50 2 26.00"`
  - Resultado de `resolve_browser_event_kickoff`: `2026-11-17T22:59:41.738633+00:00` (`method=BWIN_EVENT_COUNTDOWN_MINUTES`, diferencia `0.30 min` vs API kickoff `2026-11-17T23:00:00+00:00`, tolerancia `4 min`).
  - `bwin_market_preflight.time_evidence`: `COUNTDOWN_TIME_CORROBORATED` (`difference_minutes=0.3`).
- **Regresión `7909714`** ([`outputs/matches/7909714/inputs/bwin_metadata_20261117T145004321384Z.json`](../../outputs/matches/7909714/inputs/bwin_metadata_20261117T145004321384Z.json), SHA-256 `b659731f72940aa07f613d6094f366f4ad99baff9d5afb2ef450dc5d9f6dea06`):
  - `text`: `"Chile Colombia Mañana / 0:00 1 3.70 X 3.25 2 2.00"` capturado a `2026-11-17T14:50:04.312402+00:00`.
  - Resultado: `2026-11-17T23:00:00+00:00` (`method=BWIN_VISIBLE_HOY_MANANA_TIME`), `DISPLAY_TIME_CORROBORATED` (`difference_minutes=0.0`).
- **Expediente histórico `7909713`**: intacto (`INDEPENDENT_BWIN_TIME_UNVERIFIED_CONTEXT_PENDING`, `BLOQUEADO_PREPARTIDO` en `validacion_7909713.json`).

### 2.2 Justificación de tolerancia y controles verificados

- **Tolerancia de 4 minutos en countdown**: Bwin trunca/redondea la cuenta atrás a minutos enteros (ej. `56 min 18.26 s` aparece como `56 min` -> `0.30 min` de error por truncado) más la pausa de renderizado (~2.5 s) y posible desfase de reloj de hasta ~2 min. El límite de `4 min` coincide con el umbral ya establecido en `prematch_single.py:346` (`tolerance = 4 if evidence.get('status')=='COUNTDOWN_TIME_CORROBORATED' else 3`).
- **Endurecimiento añadido en Iteración 1.1**:
  1. `match_identity.py:228-246`: exige que todo elemento de `catalog_observations` sea `dict` con `bwin_event_id` y `bwin_url` exactos; en `method == 'BWIN_EVENT_COUNTDOWN_MINUTES'` exige `observed_at_utc` con zona horaria, `countdown_minutes` en `1..180`, y verifica `abs((obs_dt + timedelta(minutes=cd_mins)) - ko_dt) <= 1.0` para impedir la manipulación de `kickoff_utc` copiando API-Football.
  2. `validate_prematch.py:1221-1240`: acota `tol = min(4.0, ...)` y comprueba aritmética `observed_at_utc + countdown_minutes == kickoff_utc` y `observed_at_utc <= kickoff_utc`.
  3. Cobertura de cambio de día (`23:50 CET + 20 min -> 00:10 CET`), cambio horario CET/CEST (`fold=0 != fold=1` rechazado en relojes locales ambiguos y suma UTC inequívoca en countdowns), eventos incompatibles, textos `LIVE`, timestamps ausentes y expresiones mezcladas ambiguas.

---

## 3. Auditoría P0-B: `refresh-odds` y sincronización de Shadow

### 3.1 Defecto original demostrado en `7909713`

- En `outputs/matches/7909713/single_execution.json`: `refresh-context` (`22:34:13 UTC`) ejecutó Shadow sobre la captura previa (`@1.07`, 452 selecciones); `refresh-odds` (`22:48:54 UTC`) sustituyó `cuotas_bwin_7909713.txt` (`@1.055`, 356 selecciones), archivó la carpeta `shadow/` previa pero **no invalidó `shadow/shadow_summary_7909713.json` ni reejecutó `run_shadow_for_event_dir`**.

### 3.2 Endurecimiento añadido en Iteración 1.1

Durante la auditoría se detectaron y corrigieron tres brechas residuales de P0-B:
1. **`bet_engine/orchestration/adapters.py:256-267`**: si `run_shadow_for_event_dir` fallaba (por ejemplo por `ODDS_MANIFEST_SHA_MISMATCH`), dejaba el `shadow_summary_<eid>.json` previo en disco. Ahora, en ejecuciones no históricas (`not historical`), archiva y elimina cualquier `shadow_summary_<eid>.json` obsoleto antes de devolver `ok=False`.
2. **`prematch_single.py:625-635` y `767-776`**:
   - `cmd_refresh` pasa `min_lead_minutes=max(REFRESH_MIN_LEAD_MINUTES, int(args.min_refresh_lead_minutes))` en lugar de `0` a `run_shadow_for_event_dir`, preservando la barrera PREMATCH dentro de Shadow.
   - `cmd_refresh_context` elimina `shadow_summary_<eid>.json` y `CODEX_<eid>.md` (tras respaldarlos en `previous_outputs`) antes de reejecutar Shadow, evitando que sobreviva un resumen obsoleto si Shadow falla.
3. **`prematch_case.py:436-450`**: `_evaluate_closure` ahora añade `ODDS_MANIFEST_HASH_MISMATCH` y `SHADOW_STALE_VS_MANIFEST` a `closure_reasons` cuando `status == 'FINALIZADO'`, impidiendo que un cierre con cuotas manipuladas o Shadow desincronizado figure como `final_prematch_valid=True`.

### 3.3 Distinción explícita de los cuatro timestamps de trazabilidad

En `shadow_summary_<eid>.json`, `dossier_<eid>.json` (`timestamps`), `single_execution.json`, `manifest.json` y `CODEX_<eid>.md` se registran por separado:
- **Captura de cuotas Bwin**: `odds_captured_utc` (procedente del encabezado `Captura:` del TXT Bwin).
- **Cálculo de probabilidades Shadow**: `probabilities_computed_at_utc` (instante UTC de ejecución de `CoherentMatchModeler`).
- **Evaluación de precios Shadow**: `prices_evaluated_at_utc` (instante UTC de contraste en `BwinMarketEvaluator` contra `odds_sha256`).
- **Cierre del expediente**: `finish_completed_at` / `closure_checks.finish_completed_utc`.
