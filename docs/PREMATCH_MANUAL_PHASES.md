# PREMATCH: ejecución manual por fases

Verificación realizada: 147 pruebas locales, 145 aprobadas y 2 omitidas por
evidencia histórica opcional no disponible. Compilación, ayuda de los CLIs y
`git diff --check` correctos. No se adquirieron datos ni se seleccionó un evento
real nuevo; las pruebas usan fakes y replays locales en directorios temporales.

Regresión reproducible del flujo nuevo, sin proveedores:

```bash
PYTHONPATH=.:test .venv/bin/python -B -m unittest test_manual_prematch_flow test_select_single_match test_preflight_timing_v0_3_1 test_prematch_case test_market_quality_minimal test_prematch_data_acquisition test_prematch_daily_csv test_prematch_tactical_signal_engine test_bwin_discovery_v31 test_bwin_discovery_v3 test_discovery_policy_blocks test_prematch_bwin_phase3 test_prematch_commercial_join test_prematch_candidate_selector_v1
```

Cada bloque se ejecuta por separado y deja archivos para el siguiente. Ninguno
lanza automáticamente las ocho fases. Los comandos se ejecutan desde
`/Users/antonio/Projects/bet-tactical-engine`. Este documento no acredita una
ejecución online: se han verificado interfaces y contratos con pruebas locales.

## Nueva ejecución

En la misma terminal, configurar la fecha futura de inicio de la ventana. El
perfil normal y sus horas se leen de `config/prematch_windows.json`.

```bash
cd /Users/antonio/Projects/bet-tactical-engine
FECHA=2026-11-15
PY="$PWD/.venv/bin/python"
mkdir -p outputs/discovery/manual
RUN="$(mktemp -d "$PWD/outputs/discovery/manual/${FECHA}_XXXXXXXX")"
API_BUDGET=1600
CAPTURE_BUDGET=0
```

`RUN` es un nuevo identificador/directorio por ejecución. Anotar su ruta para
retomar otra terminal. Nunca apuntarlo a una evaluación histórica. El ejemplo
API_BUDGET=500 es un límite explícito de solicitudes nuevas; ajustarlo a la
cuota disponible. Si se agota, las estadísticas quedan incompletas. Las cachés
históricas válidas se siguen leyendo. CAPTURE_BUDGET=0 permite todos los
candidatos; un número positivo limita intentos, incluidos fallos. Las cuotas se
capturan de nuevo en el directorio del lote; no se adoptan handoffs anteriores.

## 1. Descubrir y clasificar

```bash
"$PY" -B discover_fixtures.py --date "$FECHA" --window normal --refresh --show 0 --output-dir "$RUN" &&
"$PY" -B filter_major_leagues.py --input "$RUN/fixtures.json" --policy adult-open --output-dir "$RUN"
```

Comprobar `fixtures.json` (incluido fixture_audit, inventario de toda la ventana),
`fixtures_major.json` y `league_filter_review.json`. `fixtures` contiene los
futuros PREMATCH; el inventario conserva también los no admitidos. `review` no
equivale a exclusión: la fase siguiente lo incorpora expresamente. `--refresh`
afecta al descubrimiento actual, no borra cachés históricas.

## 2. Estadística colectiva e individual

```bash
"$PY" -B preselect_matches.py --input "$RUN/fixtures_major.json" --include-review --max-matches 0 --mode full --history-limit 20 --detail-limit 8 --request-budget "$API_BUDGET" --output-dir "$RUN/statistics" --confirm-external &&
"$PY" -B prematch_phase2.py --preselection "$RUN/statistics/preselection.json" --cache-root outputs/history_recovery_v2/player_directed_20261109/acquired_cache --output-dir "$RUN/phase2"
```

Comprobar `statistics/preselection.json`: matches, api_errors,
api_request_ledger, fixture_evaluation_ledger, run_status y cobertura por métrica.
Después `phase2/coverage_matrix.json`, `pattern_discovery.json`,
`research_queue_sport.json` y los expedientes por fixture. Fase 2 es local.
El cache-root indicado existe en el repositorio y se lee, no se renueva; puede
sustituirse por otra caché Fase 1 compatible. No garantiza cobertura de jugadores
para los nuevos fixtures. Ausencia de jugadores, exposición, minutos o duelos
compatibles queda registrada; no se completa con datos supuestos.

## 3. Ranking estadístico preliminar

```bash
"$PY" -B prematch_pattern_engine.py --preselection "$RUN/statistics/preselection.json" --top 0 --output-dir "$RUN/patterns" &&
"$PY" -B prematch_tactical_signal_engine.py --preselection "$RUN/statistics/preselection.json" --pattern "$RUN/patterns/pattern_engine.json" --output-dir "$RUN/preliminary"
```

Comprobar `patterns/pattern_engine.json`, `pattern_evidence.csv` y
`preliminary/tactical_signal_engine.json`, especialmente statistical_ranking y
los expedientes preliminary/fixtures/. Esta fase no recibe cuotas y no decide
el partido final. Las puntuaciones son descriptivas; no probabilidades ni EV.

## 4. Identificación Bwin

```bash
"$PY" -B bwin_discovery.py --input "$RUN/statistics/preselection.json" --top 0 --headless --max-country-pages 8 --max-scrolls 12 --timeout 12000 --inspection-retries 1 --output-dir "$RUN/bwin" --confirm-external
```

Comprobar `bwin/bwin_discovery.json`, `.md` y `catalog_observed.json`. Pendiente de
hora/clasificación, catálogo incompleto y no observado son situaciones distintas
de identidad contradictoria. NOT_FOUND no demuestra ausencia de oferta Bwin.

Revisión manual opcional, sin red:

```bash
"$PY" -B bwin_market_preflight.py --discovery "$RUN/bwin/bwin_discovery.json" --write-identity-review "$RUN/identity_review.json"
```

Editar decisions usando el ejemplo de la plantilla: fixture_id, decisión
CONFIRM/CORRECT/REJECT, reviewer, reviewed_at con offset y evidence. CORRECT
incluye event con ID, URL, texto observado y timestamp. La revisión queda
vinculada al SHA de origen, no fuerza VERIFIED ni vence contradicciones.
Para usarla, añadir `--identity-review "$RUN/identity_review.json"` a fase 5 y
usar `capture/reviewed_discovery.json` como discovery en fase 7.

## 5. Capturar el lote

```bash
"$PY" -B bwin_market_preflight.py --discovery "$RUN/bwin/bwin_discovery.json" --preselection "$RUN/statistics/preselection.json" --top 0 --exploration --run-scraper --refresh-existing --immutable-capture --max-captures "$CAPTURE_BUDGET" --capture-timeout-seconds 240 --output-dir "$RUN/capture" --confirm-external
```

Comprobar `capture/market_preflight.json`: batch_summary, pendientes por
presupuesto, fallos reintentables, warnings y final_validation_reasons por
fixture. Revisar `market_selection_review.csv`, `market_research_shortlist.csv`,
`market_family_readiness.csv` y captures/ por Event ID con TXT y scraper.log.
Los snapshots canónicos anteriores no se reemplazan, aunque estén corruptos.

Reanudación progresiva: repetir exactamente el comando con `--resume` y el
presupuesto deseado. Solo reutiliza capturas frescas de este lote con los mismos
hashes; reintenta los fallos y captura los pendientes. Si cambian los inputs,
usar otro directorio de captura. No reutiliza cuotas de otra jornada.

## 6. Cruce estadístico-comercial local

```bash
"$PY" -B prematch_opportunity_scanner.py --preselection "$RUN/statistics/preselection.json" --pattern "$RUN/patterns/pattern_engine.json" --preflight "$RUN/capture/market_preflight.json" --exploration --output-dir "$RUN/commercial"
```

Comprobar `commercial/opportunity_scan.json`, `opportunity_review.csv` y
`opportunity_ranking.csv`. RESEARCH_ONLY acredita lectura/coherencia para
explorar, no identidad final. Los mercados sin modelo o calibración conservan
sus estados pendientes. Los errores de un fixture no impiden analizar otros;
un error de hashes o estructura de inputs requiere corregir el origen.

## 7. Selección comparativa para investigación

```bash
"$PY" -B select_single_match.py --preselection "$RUN/statistics/preselection.json" --discovery "$RUN/bwin/bwin_discovery.json" --preflight "$RUN/capture/market_preflight.json" --scanner "$RUN/commercial/opportunity_scan.json" --research-only --output-dir "$RUN/selection"
```

Comprobar `selection/selection_decision.json`, `selection_ranking.csv`,
`selection_ranking.md` y `research_selection.json`. Si queda presupuesto sin
ejecutar, un recorte --top o presupuesto estadístico agotado, el resultado es
INCOMPLETE_COMPARISON, sin ganador. Completar fase 5 y repetir 6–7. Los fallos
individuales ya intentados permanecen reintentables y no impiden comparar los
demás; el resumen declara esa cobertura parcial. No se crean selected_match.json
ni picks. La fórmula del selector existente no se modifica.

## 8. Preparar análisis profundo local

```bash
"$PY" -B prematch_case.py prepare-research --selection "$RUN/selection/research_selection.json" --output-dir "$RUN/deep_research"
```

Comprobar `deep_research/research_packet.json` y `research_brief.md`: fixture,
estadística, cuotas exactas, hashes, advertencias y pendientes finales. Rechaza
partidos iniciados, comparación incompleta y archivos modificados. El destino
es write-once. Es el expediente para investigación cualitativa, no una adopción
operativa ni un salto a finish.

## Validación final posterior

Tras aportar evidencia cualitativa y resolver identidad/hora/categoría,
continuar por las interfaces existentes del caso canónico. La selección
operativa sigue usando `select_single_match.py` sin --research-only y exige
preflights actuales, hora y contratos válidos. `prematch_single.py check`,
`prepare`, `refresh-odds`, `retry-context`, `refresh-context` y `finish` mantienen
sus controles y se ejecutan manualmente para el seleccionado. Un expediente
exploratorio no es un selected_match válido para esos comandos. Si persiste
algún pendiente, conservarlo como pendiente de validación final. No se ha
ejecutado ninguna de estas operaciones durante esta corrección.
