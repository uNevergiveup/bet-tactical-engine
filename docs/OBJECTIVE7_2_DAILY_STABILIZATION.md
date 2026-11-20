# Objetivo 7.2 — estabilización del flujo diario PREMATCH

## Resultado

Se mantiene `prematch_daily.py` como único comando. La corrección se validó
exclusivamente con artefactos locales y copias temporales; no se ejecutaron
API-Football, Bwin, `finish` ni generación automática de picks.

## 1. Exportación CSV

### Causa

`prematch_daily_csv.export_daily()` localizaba correctamente `fixtures.json`
dentro de una evaluación aislada, pero no podía reconstruir la ventana. El
campo del snapshot contiene una hora civil sin offset (`Europe/Madrid`) y el
fallback esperaba que el directorio padre se llamara
`YYYY-MM-DD_HHMM_HHMM`. Desde 7.1, el padre se llama con el ID de evaluación.
La función retornaba `[]` antes de publicar CSV, JSON o Markdown.

### Corrección

En el layout 7.1 se leen `window_start_madrid` y `window_end_madrid` del
`evaluation_manifest.json`, que contiene offsets explícitos. El fallback por
nombre se conserva para layouts históricos.

Replay sobre una copia temporal del piloto:

- 75 filas únicas dentro de la ventana;
- CSV, JSON y Markdown publicados;
- estados procedentes del ledger, preflight, scanner y handoff, sin completar
  vacíos con ceros ni inventar resultados.

## 2. Ranking estadístico y evidencia 6.1

Los 14 fixtures del piloto no coinciden con ninguno de los ocho Fixture IDs de
`objective6_1_international_final_v1`. `NO_COMPATIBLE_FIXTURE` y los 14
`NOT_AVAILABLE_FOR_FIXTURE` son legítimos para la evidencia ampliada 6.1.

El defecto estaba en la presentación: `statistical_research_ranking` se
ordenaba únicamente por las clasificaciones 6.1. Sin dossier compatible, los
14 registros quedaban empatados y ocultaban la evidencia descriptiva local.

Ahora cada fila distingue:

- `statistical_basis=CURRENT_PRESELECTION_DESCRIPTIVE_ENGINE`;
- score, señales, familias, cobertura y contradicciones del motor diario;
- `expanded_6_1_evidence`, que puede permanecer no disponible;
- ausencia deliberada de preparación comercial y de picks.

El replay local produjo 14 filas. Primeros tres por evidencia estadística:

1. Fixture 1520978 — 82,10; una señal.
2. Fixture 1559703 — 81,91; una señal.
3. Fixture 1646937 — 81,70; una señal.

El ranking comercial continúa separado en `tactical_selection_ranking`.

## 3. Revalidación Bwin del Fixture 1520978

El log original
`bwin_discovery_20261112T201533763723.log` demuestra un fallo técnico:

```text
KeyError: 'name_score'
```

La ruta directa creada desde el catálogo 5.1 se enviaba a `summarize()` sin
pasar por la normalización que añade `name_score`, orden, categoría y
similitud de competición. No fue un rechazo de identidad.

La ruta persistida pasa ahora por `match_catalog()` antes de `summarize()`. Así
se aplican las mismas comprobaciones semánticas que al catálogo normal y la
inspección fresca de página sigue siendo obligatoria. La regresión recupera el
Event ID 7911881 con score exacto 1,0 y rechaza nombres no relacionados.

## 4. Métricas completas

El `phase_metrics.json` anterior comenzaba dentro de `scan()` y por ello solo
registró 47,9374 segundos. La preparación quedó fuera.

La nueva versión registra:

- `daily_total_elapsed_seconds`: tiempo monotónico del comando completo;
- `exclusive_phase_seconds`: resolución, preparación, predictor opcional,
  scan, market mapping, CSV y orquestación residual;
- `scan_internal_phase_seconds`: detalle interno anterior, anidado;
- `subprocess_component_seconds_nested`: API fixtures/históricos,
  procesamiento estadístico, descubrimiento Bwin, preflight y scanner.

Las fases exclusivas suman una sola vez el total. Los detalles internos y de
subprocesos se etiquetan expresamente como anidados y no se vuelven a sumar.

Como diagnóstico del piloto congelado, los logs contienen 19,7841 s de
fixtures/históricos, 0,1639 s de scripts estadísticos, 170,8045 s de
descubrimiento Bwin, 32,7856 s de preflights y 0,0624 s de scanner. Estas
cifras son diagnósticos anidados; el próximo piloto registrará directamente el
total monotónico completo.

## Pruebas

`test_prematch_objective7_2.py` cubre las cuatro correcciones. La suite global
terminó con **722 pruebas correctas y 10 omitidas**. Puede repetirse con:

```bash
.venv/bin/python -m unittest discover -q
```

No se modificaron umbrales, probabilidades, filtros comerciales, reglas de
identidad ni generación de picks.

## Comando de piloto

```bash
.venv/bin/python -B prematch_daily.py \
  --online --confirm-external --window normal \
  --horizon-hours 4 --history 20 --details 8 \
  --discovery-retries 1 \
  --bwin-identity-catalog \
  outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json
```
