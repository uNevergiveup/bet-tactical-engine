# Motor PREMATCH V2 — Fase 1: auditoría, integración y contratos

Fecha: 2026-11-09. Alcance implementado: orquestación y reproceso offline.
El descubrimiento avanzado, LLM y calibración V2 descritos aquí son diseño pendiente.
Las reglas de AGENTS.md orientadas al analista final por Event ID no activan ese
análisis: esta tarea autoriza explícitamente la auditoría de arquitectura y sus tests.

## Auditoría de capacidades reales

| Componente y funciones | Existe y se conserva | Parcial o ausente |
|---|---|---|
| `prematch_daily.py`: `main`, `run_optional_predictor`, `run_optional_market_mapping` | Ventana 15–03 Madrid, lock, etapas opcionales y preparación reutilizable | Mapping se ejecutaba antes de `scan`; fuente implícita legacy, sin enlace al run del predictor. Corregido en esta fase |
| `prematch_runtime.py`: `scan`, `verified_preflight` | Revalidación de originales, SHA, identidad, cuotas, T−45; reuso antes de captura, presupuestos explícitos y auditoría comparativa | El scan aún utiliza selección táctica legacy; no es el selector estadístico V2 |
| `prematch_predictive_engine.py`: `run`, `fixture_models`, `walk_forward`, `verify_existing_run` | Fuentes congeladas, SHA/write-once, disponibilidad de resultados previa al cutoff, Poisson/NB y walk-forward preliminar, equipos/localía y siete métricas FT | Probabilidades de matchup sin calibración comercial demostrada. No descubre interacciones arbitrarias ni duelos individuales validados |
| `prematch_pattern_discovery.py`: `build_discovery`, `explicit_patterns`, `exploratory_patterns`, `quality`, `build_queue` | Producción/concesión por sede, tendencias, interacciones predefinidas, cobertura/contraevidencia, cola por patrón y fixture | Las interacciones son ejemplos codificados; no búsqueda general con corrección por pruebas múltiples |
| `prematch_predictive_evidence_ranking_v1.py`: `category_name`, `row_quality`, `aggregate`, `build` | Métricas shots/on_target y yellow diferenciadas; ranking heurístico sin odds | Constantes 290/8021 y nombres 20261108: no ejecutable tal cual para el día 9. `AVAILABLE` no demuestra estabilidad; goals incluye xG. No se modifican fórmulas en esta fase |
| `prematch_candidate_selector_v1.py`: `select` | Unión TOP20/TOP10, deduplicación, filas pendientes visibles | CSV/fecha/universo fijos al día 8; posiciones guardadas solo TOP10; marca temporal no consulta estado proveedor. `elegible_para_scraping` era true sin Event ID: ahora false hasta vinculación; el selector aún no ingiere bindings |
| `prematch_phase1.py`: `Phase1ApiClient`, `identity_map`, `StatisticalCollector` | Caché, presupuesto, paginación, evidencia original, adquisición API separada de binding Bwin; fixtures/statistics/players/lineups/events sujetos a cobertura | Contexto táctico causal y verificación oficial de XI no se deducen de estas respuestas |
| `bwin_discovery.py`, `bwin_market_preflight.py`, `market_quality_gate.py` | Catálogo, inspección específica, hora visible fechada/absoluta, contratos y triples reales, unión estadística por fixture/equipos/género/kickoff | `DISPLAY_TIME_CORROBORATED` no equivale a tiempo absoluto machine-verified de Fase 1. Equivalencias de competición requieren evidencia explícita |
| `prematch_market_mapping_v1.py`: `_preflight_identity`, `_load_market_rows`, `_compatible`, `map_markets` | Enlace de snapshot SHA/evento/equipos/kickoff/unión y mercado por equipo; write-once e idempotencia | No es calibración ni permiso de apuesta. `_compatible` no impone explícitamente período FT; CSV derivado no acredita por sí solo integridad hasta TXT por selección. Duplicar preflights del mismo fixture bloquea. No se amplía este motor en Fase 1 |
| Fases 2.1–2.5, `prematch_release.py`, `prematch_single.py`, validadores | Estadísticas individuales, históricos/SOS, interpretación, research-kit/manual, gates check/prepare/finish | No hay contrato V2 aplicado de expediente reutilizable, descubrimiento universal ni auditoría de causalidad automática |

No se asume que disponer de una métrica API implica su cobertura. Presión, centros,
regates, coordenadas, estados del marcador, eventos y minutos requieren comprobar
endpoint, identidad, denominador y disponibilidad por fixture. Una formación no
demuestra comportamiento. Un xG no es un gol observado. Se conservan null y ceros.

## Evidencia real del 9 de noviembre

Base: `outputs/discovery/2026-11-09_1500_0300/prospective_trial/`.

* `patterns/pattern_engine.json`: **720 patrones, 45 fixtures**; detector legacy.
* `predictive/1.0-mvp/20261109T012758+0100_ada40f30a2ac_code-810f03a2ea4f/predictive_patterns.json`:
  **1.048 patrones descriptivos, 45 fixtures, 630 modelos FT**. Los 630 son
  45 × 2 equipos × 7 métricas; no se suman a los patrones.
* `market_mapping_v1/market_mapping.json` original: 720 PENDING_ODDS;
  `data_sources.preflights=[]`, fuente legacy. Se conserva byte a byte.
* `daily_runs/20261109T020353042168/preflight/*/market_preflight.json`: cinco
  capturas posteriores al punto de ejecución de mapping. Los cinco TXT actuales
  coinciden con SHA declarado y los cinco joins están MATCHED_FIXTURE_TEAMS_GENDER_KICKOFF.

| API fixture | Bwin Event | Triples | Patrones canónicos | Resultado offline nuevo |
|---|---|---:|---:|---|
| 1549860 | 7907189 | 438 | 39 | 2 MATCHED, 4 PENDING_VALIDATION, 20 sin mercado, 13 unsupported |
| 1565299 | 7909481 | 243 | 44 | 2 MATCHED, 4 PENDING_VALIDATION, 25 sin mercado, 13 unsupported |
| 1632059 | 7909562 | 149 | 43 | 31 sin mercado, 12 unsupported |
| 1632060 | 7909563 | 145 | 38 | 26 sin mercado, 12 unsupported |
| 1645541 | 7909565 | 165 | 31 | 21 sin mercado, 10 unsupported |

Nuevo total: **1.048 = 4 MATCHED + 8 PENDING_VALIDATION + 123 MARKET_NOT_AVAILABLE
+ 60 UNSUPPORTED_MARKET + 853 PENDING_ODDS**. El 4 cuenta patrones con enlaces,
no apuestas ni cuatro cuotas individuales. MARKET_MATCHED significa que pasó el
contrato V1 de mapping; no se promueve a identidad absoluta verificada ni a
decisión actual accionable. Reproceso de snapshots: no se consulta LIVE, no se
rejuvenecen las capturas y no se afirma que sus precios estén vigentes.

## Correcciones implementadas

1. `main`: predictor → scan/adquisición-reuso de preflights → mapping. Si scan falla
   no se publica como mapping final un resultado anterior a las capturas.
2. Mapping recibe el directorio del predictor recién generado. Sin él se busca
   un único run congelado cuyo source SHA coincida con la preselección actual y
   cuyo SHA de patrones coincida con el manifiesto. Ambigüedad exige ruta explícita.
   `--market-mapping-patterns` explícito conserva compatibilidad legacy trazable;
   nunca se mezclan ambos detectores.
3. Selección automática de preflights solo en directorios originales, excluyendo
   comparison_audit. Repeticiones del mismo fixture/evento usan el original más
   reciente; dos Event IDs diferentes exigen revisión. Un SHA inválido permanece
   bloqueado por mapping; no se reemplaza por una captura antigua silenciosamente.
4. Salidas en `market_mapping_v1/runs/<hash-de-entradas-y-código>/`. El mapping
   anterior no se sobrescribe. Cada run liga patrones, preflights, CSV y código;
   el módulo vuelve a comprobar SHA del TXT.
5. `--market-mapping-only --date ...`: ruta offline temprana, sin prepare, scan,
   predictor ni exportaciones de otras fases. Se rechazan opciones de adquisición.
6. Selector: futuro sin binding sigue visible como investigable estadísticamente,
   pero no se marca autorizado para scraping.

## Contrato funcional V2 (diseñado, no implementado)

Esquema: `config/schemas/prematch_v2_dossier.schema.json` (JSON Schema 2020-12).
Sin dependencia ni validador V2 instalado por esta tarea. El esquema describe
estructura; el futuro validador debe imponer invariantes semánticas adicionales:

* Todos los IDs y referencias evidence_id/pattern_id existen y son únicos. Event ID
  nunca es fixture_id. Identidad: equipos y orden, categoría/género, competición,
  temporada, kickoff y evidencia Bwin específica. Club/selección usan la misma
  entidad con `team_kind`; sin transferir automáticamente baselines entre ambos.
* `available_at <= source_cutoff <= evaluation_at < kickoff` para uso prospectivo.
  Fecha de adquisición distinta de fecha del encuentro/disponibilidad. Si ésta no
  está acreditada, reconstrucción retrospectiva o pendiente, jamás prospectiva.
* Históricos excluyen el objetivo, futuros al cutoff y resultados aún no disponibles.
  n válido <= solicitado; partidos, minutos y unidades documentados; null no es cero.
* Hipótesis identifica patrón y estadísticas originales, IDs históricos, condiciones,
  mecanismos posibles, evidencia favorable/contraria con fuente fechada y SHA,
  verificación, invalidadores, aplicabilidad al rival y preguntas pendientes.
* Python adquiere/limpia/calcula/contrasta asociaciones y mantiene trazabilidad.
  LLM interpreta mecanismos y propone hipótesis/refutaciones. Modelo probabilístico
  estima solo mercados concretos con calibración cronológica demostrada.
* Codex manual usa exportación del expediente y devuelve hipótesis con
  producer=CODEX_MANUAL; importación verificará fuentes, IDs, fechas y contraevidencia.
  Un futuro adaptador LLM_API podrá usar el mismo esquema. Ningún LLM se ejecuta
  automáticamente ahora; ninguna explicación ajusta probabilidades arbitrariamente.

### Descubrimiento y validación

El futuro generador de asociaciones parte de métricas disponibles y su ontología
(equipo/jugador/duelo, producción/concesión, período, sede, estado del marcador),
en lugar de enumerar solamente pares manuales. Exige exposición/minutos y
comparabilidad por competición/temporada/categoría. Para duelos individuales,
coincidencia de minutos y rol/sector acreditados; sin posiciones no inventar duelos.
Las condiciones de marcador se aprenden de históricos cerrados con cronología;
no usar el estado LIVE del objetivo.

Registrar todas las asociaciones probadas, predefinir ventanas y holdouts por fecha,
separar entrenamiento/selección/calibración/test; ajustar pruebas múltiples (p.ej.
FDR por familia), bootstrap por fixture/equipo, sensibilidad a extremos y regímenes.
No usar patrones elegidos tras observar resultados como confirmación fuera de muestra.
Mantener EXPLORATORY mientras no supere validación; reportar calibración, discriminación,
baseline, intervalos y degradación por liga. El ranking sigue siendo prioridad de evidencia.

### Bwin temprano y evaluación comercial

Ramas estadísticas y catálogo/capturas pueden correr en paralelo con presupuestos
separados y uniones por identidad. Barrera de sincronización antes del mapping
definitivo: quedan pendientes explícitos si alguna rama no terminó. Reusar caché y
snapshots íntegros antes de consultar. Captura nueva solo hasta T−45 como ahora.

Cada selección: fixture/evento, SHA/fecha, mercado literal, equipo/jugador,
período, línea, dirección, cuota y liquidación. Ausencia en una captura significa
ausencia observada con alcance y hora, no inexistencia universal. Cuota <1,20:
descarte comercial configurable; >=1,20: evaluable sujeto a datos/modelo/identidad.
No deducir señal Over de cualquier aumento de medias ni confundir goles/xG.
Offsides permanece táctico experimental hasta cobertura y validación Bwin.
Rankings por selección/mercado y agrupados por fixture; ningún límite fijo 2–3.

Estados propuestos: SUPPORTED_COMMERCIAL_CANDIDATE, EXPERIMENTAL_CANDIDATE,
PENDING_INFORMATION, DISCARDED_WITH_CAUSE, BLOCKED. Cada estado tiene razón y
evidencias. Cualitativo opcional ausente no bloquea automáticamente. BLOCKED se
reserva a identidad, corte, integridad u otra dependencia esencial contradicha.

### Flujo manual y actualización XI

Cada etapa exportará stage_report: qué hizo/encontró, faltantes, razón del siguiente
paso, comando concreto, obligatoriedad (OBLIGATORY/RECOMMENDED/OPTIONAL), entradas
externas Grok/Perplexity/manuales y artefactos reutilizados con hashes. Usuario
ejecuta cada comando; no se agenda ni ejecuta IA externa.

A T−30 se diseñará una actualización separada del XI, sin recapturar cuotas ni
repetir SOS: API `/fixtures/lineups` con caché/presupuesto o entrada manual con
URL/documento oficial, fecha, SHA e identidad de jugadores. Distinguir proveedor,
probable y oficial corroborado. Reconciliar 11+11 y suplentes, no inventar IDs.
Versionar expediente y invalidar solo hipótesis dependientes. Revalidar kickoff y
estado antes/después; bloquear LIVE. No convertir esto en permiso para capturar
Bwin tras T−45. Este flujo es diseño; no hay comando de actualización V2 aún.

## Plan de implementación por impacto y dependencias

| Orden | Trabajo sobre módulos existentes | Dependencias / aceptación |
|---|---|---|
| 1 (siguiente fase) | Endurecer contrato de mapping en `prematch_market_mapping_v1.py` y preflight: FT, identidad por evidencia, reconstrucción de filas desde TXT, hash de CSV y pattern_id | Tests de períodos incompatibles, alias/competición, falsificación de CSV, evento cruzado, antigüedad; ninguna promoción de binding provisional |
| 2 | Generalizar `prematch_predictive_evidence_ranking_v1.py` y selector por fecha/universo; consumir binding verificado; conservar todas las posiciones; goals/xG visibles por separado | Reutiliza fórmulas congeladas; cuenta y fuente por snapshot, cero acceso Bwin sin binding |
| 3 | Extender `prematch_phase1.py` y Fase 2.1: exposición de jugadores, eventos por minuto, cobertura/calidad y roles documentados; expediente reutilizable | Schema/validador semántico y anti-leakage; ampliar solo métricas realmente disponibles |
| 4 | Extender discovery para asociaciones y condiciones cronológicas; búsqueda Python con catálogo dinámico de variables | Muestras suficientes, pruebas múltiples, holdout por fecha, trazabilidad de todos los tests |
| 5 | Puente manual LLM/research-kit y actualización XI T−30 | Expedientes válidos y fuentes fechadas; evidencia contraria/importación, ningún número probabilístico LLM |
| 6 | Validación/calibración por mercado/línea/período y decisiones posteriores | Dataset cronológico independiente, comparación baseline, intervalos y auditoría comercial; sin apostar por obligación |

## Reproducción offline

Desde `/Users/antonio/Projects/scraper`:

```bash
.venv/bin/python prematch_daily.py --date 2026-11-09 --market-mapping-only
```

No invoca adquisición. Si existen varios runs compatibles, indicar la fuente:

```bash
.venv/bin/python prematch_daily.py --date 2026-11-09 --market-mapping-only \
  --market-mapping-patterns outputs/discovery/2026-11-09_1500_0300/prospective_trial/predictive/1.0-mvp/20261109T012758+0100_ada40f30a2ac_code-810f03a2ea4f/predictive_patterns.json
```

Para delimitar preflights se puede repetir `--market-mapping-preflight <ruta>`;
sin flags se excluyen copias de auditoría y se usan originales del día solicitado.
La fuente explícita permite auditar un detector legado aisladamente sin mezclarlo.
`--market-mapping-only` sigue siendo reproceso histórico, no autorización actual.

Tests de esta fase: `test_prematch_v2_integration.py`; regresión completa indicada
en el informe de entrega. No ejecutar el comando diario de adquisición para esta
auditoría. No se regeneran CSV antiguos, cuotas, análisis ni snapshots deportivos.

## Verificación ejecutada

**219 tests PASS**, sin API ni scraping. Incluye los 214 anteriores y cinco nuevos:
orden predictor/scan/mapping, fuente del mismo run sin duplicar copias de auditoría,
preservación de mapping anterior e idempotencia, ausencia de fallback legacy,
rechazo de SHA del predictor alterado y ruta offline sin adquisición (algunos
tests comprueban más de una propiedad). La regresión existente del selector
ahora comprueba que identidad pendiente implica cero permisos de scraping.

Repetición sobre datos reales: 12 artefactos de entrada idénticos antes/después
(5 preflights, 5 TXT, predictor y mapping antiguo). Dos mappings consecutivos
producen SHA `fd4d5548eebb1b64825791ccce24a68df7d4376896fad16815eade51e6164a1a`.
Salida: `market_mapping_v1/runs/21aa5dee615e27ea152f/market_mapping.json`.
Schema JSON parseable y referencias locales resueltas; esto no es una validación
semántica de expedientes V2. `git diff --check` PASS.

```bash
.venv/bin/python -m unittest -q \
  test_prematch_v2_integration.py test_prematch_candidate_selector_v1.py \
  test_prematch_predictive_evidence_ranking_v1.py test_prematch_market_mapping_v1.py \
  test_prematch_predictive_engine.py test_prematch_prospective_trial.py \
  test_prematch_daily.py test_prematch_phase1.py test_prematch_phase2.py \
  test_prematch_phase3.py test_prematch_pattern_discovery_v2.py \
  test_prematch_tactical_signal_engine.py test_prematch_temporal_windows.py \
  test_prematch_predictive_temporal_guard.py
```
