# PREMATCH Steps Runbook (`prematch_steps.py`) — BET ENGINE V4 (P0)

## 1. Auditoría del flujo real (módulos inspeccionados)

| Módulo | Responsabilidad actual | Acoplamiento / Reutilización |
|---|---|---|
| `discover_fixtures.py` | Descubrimiento de fixtures PREMATCH vía API-Football (`fixtures.json`, `.csv`, `.md`, `fixture_audit`). | Independiente; reutilizado por `discover`. |
| `filter_major_leagues.py` | Filtro deportivo (`top-tier-and-senior-cups`, `adult-open`, `domestic-top-tier`) con `ELIGIBLE`, `REVIEW`, `EXCLUDED`. | Independiente; reutilizado por `filter`. |
| `bwin_discovery.py` | Descubrimiento automático Bwin e inspección de identidad/hora/mercados. | Independiente; reutilizado por `bwin-scan` y resolución de Event ID. |
| `bwin_manual_links.py` | Registro, reconciliación y verificación online de vinculaciones manuales Bwin. | Ampliado mínimamente para resolver `Event ID` numérico desde catálogo local u online sin fabricar URLs. |
| `prematch_runtime.py` | Orquestador diario (`scan`, `discovery_for`, `verified_preflight`). | Acoplado: `scan()` exige `reusable_preparation()` (`preselection.json` + `pattern_engine.json`) antes de Bwin. |
| `prematch_daily.py` | CLI diario de alto nivel. | Acoplado a `prematch_runtime.scan()` y `prematch_prospective_trial`. |
| `preselect_matches.py` | Descarga de historiales y detalle API-Football + cobertura por familia. | Reutilizable por `fixture_ids`; usado por `stats-fetch`. |
| `prematch_tactical_signal_engine.py` | Motor de señales descriptivas, contradicciones y ranking estadístico separado. | Independiente de Bwin/EV; reutilizado por `patterns`. |
| `prematch_case.py` | Gestión de expediente por evento (`outputs/matches/<EVENT_ID>/`). | Reutilizado como referencia de estructura inmutable por evento. |
| `prematch_single.py` | Flujo por evento único (`prepare`, `refresh`, `finish`, `close`). | Flujo secuencial por evento. |
| `bet_engine/orchestration/shadow_pipeline.py` | `ShadowPipelineRunner` + `CoherentMatchModeler` (B0) + auditoría de catálogo Bwin TXT. | Reutilizado intacto por `model` y `odds-capture`. |
| `bet_engine/modeling/prospective_cand_2b3b.py` | Evaluación paralela experimental `CAND_2B3B_V1`. | Intacto; únicamente experimental dentro de Shadow. |

---

## 2. Comandos reales por etapa

Todos los subcomandos aceptan `--date YYYY-MM-DD` (o `--eval-dir <RUTA>`), `--window`, `--asof` y `--root`.
Ningún comando ejecuta automáticamente el siguiente.

1. **`discover`** (sin descarga de microestadísticas ni 20 partidos):
   ```bash
   .venv/bin/python prematch_steps.py discover --date 2026-11-19
   ```
2. **`filter`** (política `top-tier-and-senior-cups`, muestra `ELIGIBLE`, `REVIEW` y `EXCLUDED` con razones):
   ```bash
   .venv/bin/python prematch_steps.py filter --date 2026-11-19
   ```
3. **`bwin-scan`** (busca/reconcilia identidad Bwin; `--no-browser` para solo artefactos locales o `--confirm-external` para navegación Bwin):
   ```bash
   .venv/bin/python prematch_steps.py bwin-scan --date 2026-11-19 --confirm-external
   ```
4. **`bwin-list`** (cola completa de pendientes, búsqueda incompleta, conflictos y verificados):
   ```bash
   .venv/bin/python prematch_steps.py bwin-list --date 2026-11-19 --include-verified
   ```
5. **`bwin-bind`** (admite `--event-id <ID>` resolviendo URL real sin inventar slugs, o `--url <URL>`):
   ```bash
   .venv/bin/python prematch_steps.py bwin-bind --date 2026-11-19 --fixture-id 1638286 --event-id 7913441 --confirm-external
   # o con URL completa:
   .venv/bin/python prematch_steps.py bwin-bind --date 2026-11-19 --fixture-id 1638286 --url "https://www.bwin.es/es/sports/eventos/..."
   ```
6. **`bwin-verify`** (verificación online de vinculaciones manuales pendientes):
   ```bash
   .venv/bin/python prematch_steps.py bwin-verify --date 2026-11-19 --confirm-external
   ```
7. **`stats-fetch`** (solo fixtures con Bwin verificado por defecto; reanudable sin repetir descargas):
   ```bash
   .venv/bin/python prematch_steps.py stats-fetch --date 2026-11-19
   # Ruta explícita solo investigación sin Bwin (sin autorización comercial):
   .venv/bin/python prematch_steps.py stats-fetch --date 2026-11-19 --fixture-id 1638286 --research-without-bwin
   ```
8. **`patterns`** (motor de señales descriptivas, contradicciones y señales sin oferta Bwin):
   ```bash
   .venv/bin/python prematch_steps.py patterns --date 2026-11-19
   ```
9. **`odds-capture`** (captura o validación de snapshot fresco Bwin, preservando capturas previas inmutables):
   ```bash
   .venv/bin/python prematch_steps.py odds-capture --date 2026-11-19 --fixture-id 1638286 --confirm-external
   ```
10. **`reasoning-package`** (expediente autocontenido para Codex en `outputs/matches/<EVENT_ID>/codex_reasoning_input.md`, sin llamar a ninguna API LLM):
    ```bash
    .venv/bin/python prematch_steps.py reasoning-package --date 2026-11-19 --fixture-id 1638286
    # Importación posterior de artefacto cualitativo (con validación de identidad/timestamp/procedencia):
    .venv/bin/python prematch_steps.py reasoning-package --date 2026-11-19 --fixture-id 1638286 --import-qualitative ruta/cualitativo.json
    ```
11. **`model`** (ejecuta `ShadowPipelineRunner` con `B0` operativo y `CAND_2B3B_V1` experimental sin modificar umbrales ni calibración):
    ```bash
    .venv/bin/python prematch_steps.py model --date 2026-11-19 --fixture-id 1638286
    ```
12. **`status`** (resumen de artefactos presentes y siguiente etapa sugerida):
    ```bash
    .venv/bin/python prematch_steps.py status --date 2026-11-19
    ```

---

## 3. Ubicación exacta de los artefactos

Dentro de `<EVAL_DIR>` (por defecto `outputs/discovery/<YYYY-MM-DD_HHMM_HHMM>/`):
- `fixtures.json`, `fixtures.csv`, `fixtures.md` (`discover`)
- `fixtures_major.json`, `fixtures_major.csv`, `fixtures_major.md`, `league_filter_review.json` (`filter`)
- `prospective_trial/bwin/bwin_scan_reconciliation.json`, `pending_manual_bwin_queue.json`, `manual_bwin_links.json` (`bwin-scan`, `bwin-list`, `bwin-bind`, `bwin-verify`)
- `prospective_trial/enriched/preselection.json` (o `prospective_trial/research_only/preselection.json` si `--research-without-bwin`) (`stats-fetch`)
- `prospective_trial/tactical/tactical_signal_engine.json`, `TACTICAL_SELECTION_REPORT.md` (`patterns`)
- `prospective_trial/captures/<EVENT_ID>/cuotas_bwin_<EVENT_ID>_<STAMP>.txt` (`odds-capture` cuando ya existe captura canónica)
- `steps/<etapa>.json` y `steps/<etapa>.md` (resumen auditable de cada subcomando)

Por evento en `outputs/matches/<EVENT_ID>/` (o `outputs/matches/research_fixture_<FID>/` si `--research-without-bwin`):
- `inputs/cuotas_bwin_<EVENT_ID>.txt` (snapshot canónico inmutable)
- `codex_reasoning_input.md` y `codex_reasoning_package.json` (`reasoning-package`)
- `qualitative_imported.json` (`reasoning-package --import-qualitative`)
- `model/model_report_<EVENT_ID>.json` y `model/model_report_<EVENT_ID>.md` (`model`)

---

## 4. Ejemplo real de cola Bwin manual (`2026-11-19`)

Ejecución real sobre `2026-11-19` (`discover` → `filter` → `bwin-scan --no-browser` → `bwin-list`):
- `discover`: 484 registros API → 23 partidos PREMATCH en 11 competiciones.
- `filter` (`top-tier-and-senior-cups`): 11 `ELIGIBLE`, 3 `REVIEW`, 9 `EXCLUDED`.
- `bwin-list`:

| Fixture ID | Hora Madrid | País / Liga | Género | Partido | Estado | Razón |
|---:|---|---|---|---|---|---|
| `1650500` | 2026-11-19T20:30:00+01:00 | Uruguay / Primera División | M | Boston River – Deportivo Maldonado | `PENDING_MANUAL` | `NO_BWIN_SCAN_OR_MANUAL_BINDING` |
| `1638286` | 2026-11-19T21:00:00+01:00 | World / UEFA Champions League Women | F | Arsenal W – Lyon W | `PENDING_MANUAL` | `NO_BWIN_SCAN_OR_MANUAL_BINDING` |
| `1638297` | 2026-11-19T21:00:00+01:00 | World / UEFA Champions League Women | F | Barcelona W – Servette Chênois W | `PENDING_MANUAL` | `NO_BWIN_SCAN_OR_MANUAL_BINDING` |
| `1638338` | 2026-11-19T21:00:00+01:00 | World / UEFA Champions League Women | F | SL Benfica W – Roma W | `PENDING_MANUAL` | `NO_BWIN_SCAN_OR_MANUAL_BINDING` |
| `1492458` | 2026-11-19T23:30:00+01:00 | Brazil / Serie A | M | Vasco DA Gama – Internacional | `PENDING_MANUAL` | `NO_BWIN_SCAN_OR_MANUAL_BINDING` |
