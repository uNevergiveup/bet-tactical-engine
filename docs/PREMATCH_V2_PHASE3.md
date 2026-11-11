# Fase 3 — Descubrimiento avanzado V2, primera versión funcional

Implementación: `prematch_advanced_discovery_v2.py`. Tests:
`test_prematch_advanced_discovery_v2.py`. Se conserva el código de Predictor V1,
ranking, selector, Market Mapping, SOS y validadores. No hay API ni Bwin ni LLM.

## Auditoría previa y reutilización

El Predictor V1 ya aporta disponibilidad de resultados (`result_available_at`),
series y modelos congelados. El detector anterior tiene interacciones específicas
programadas. Fase 2.1 ya implementa `PlayerCache`, reconciliación de identidad,
minutos, XI, conflictos y métricas. Se reutilizan ese lector y normalizador; no se
crea un cliente API paralelo. Ranking y mapping siguen consumiendo su contrato V1.

Los snapshots 8/9 contienen `statistical_observations.home/away`, goles propios y
concedidos, métricas solicitadas selectivamente, fixture/equipos/sede/competición y
corte. La caché local conserva respuestas reales de `/fixtures`,
`/fixtures/statistics`, `/fixtures/players`, `/fixtures/lineups`, `/fixtures/events`.
También se aprovechan listas `statistics`, `lineups`, `events` embebidas en
`/fixtures?ids=...`, con el SHA del original. No se inventan nuevos endpoints.

La caché prueba disponibilidad histórica de esas respuestas, no el derecho actual
a todos los endpoints/ligas del plan contratado. Recuperar faltantes requeriría
una futura adquisición autorizada con el cliente y presupuesto existentes; no se
ha realizado ni se asegura de antemano que el proveedor tenga las métricas.

## Base histórica

Se deduplica por fixture/equipo/género, se reconcilian detalles complementarios y
se excluyen conflictos. Jugadores se identifican por fixture/equipo/player_id;
homónimos no se juntan. Cero observado se conserva; falta de dato es null o ausencia
documentada, no cero. Resultados deben estar disponibles antes del menor corte
original de los targets, usando la política conservadora existente de +3 horas
cuando no existe hora acreditada. Se excluyen el objetivo y resultados futuros.

La base contiene goles/xG/remates/a puerta/bloqueados/córners/faltas/tarjetas,
posesión/pases cuando el snapshot los aporta; jugadores con minutos, titularidad,
posición y estadísticas individuales. Los campos adicionales numéricos del crudo
individual se conservan por su ruta original. Per90 conserva el denominador de
minutos positivo y solo entra en descubrimiento con >=30 minutos observados por
partido. Totales y tasas se distinguen; no se estima exposición inexistente.

Los registros se segmentan por entidad, equipo, jugador, liga, temporada, género,
tipo club/selección y período. No se elimina una Copa por no ser la liga del objetivo;
se analiza por separado si figura en el snapshot. No se reconstruyen competiciones
no guardadas. `team_kind=UNKNOWN` se conserva si falta clasificación acreditada.
CLUB y NATIONAL_TEAM explícitos no se mezclan; no se inventa continuidad de
convocatorias, entrenadores o roles. No hay modelo específico de selección nacional
validado en estos datos.

Contexto calculado:

* Último valor **observado** por métrica en la misma competición/temporada, acabado
  antes de cada partido. No se confunde con el último encuentro realmente disputado.
* Separación HOME/AWAY; posición/titularidad/formación cuando están documentadas.
* Referencia rival anterior a cada partido: puntos por partido sobre >=4 resultados
  previos comparables; categorías >=2 fuerte, <1 débil, resto media. No usa tablas
  finales. Es un proxy limitado, no SOS calibrado ni sustituto del módulo SOS.
* Intervalo entre encuentros observados: `context.observed_gap_days`. Calendario
  incompleto impide llamarlo descanso real o carga total.
* Goles H1/H2 solo con marcadores FT/halftime coherentes del proveedor. No se reparte
  artificialmente una estadística FT. Se conservan eventos y sustituciones como
  evidencia; no se reconstruyen aún estados completos de marcador/tiempo efectivo.

## Descubrimiento genérico

Cada segmento se ordena cronológicamente. El 70% más antiguo entrena; el resto es
holdout. Un mismo instante de kickoff no se divide entre ambos. Se aprenden medianas
solo en entrenamiento para umbrales por variable. No son líneas de apuesta.

El motor genera condiciones numéricas y categóricas a partir de los campos
disponibles, sin reglas por club/jugador ni parejas de métricas específicas:

* OWN: relación entre métricas propias o persistencia entre último valor observado
  y producción posterior; incluye variables individuales cuando hay muestra.
* CROSSED: producción propia condicionada por producción/concesión contraria
  registrada en los históricos. Se añade contexto del próximo rival: distribución
  de concesión comparable respecto al mismo umbral y fixtures de referencia.
  Esto no es un modelo de matchup calibrado ni identifica duelos individuales sin
  posiciones y minutos comunes acreditados.
* CONDITIONAL: localía, titularidad, posición, formación, categoría rival y hueco
  observado de calendario, según cobertura.

Se calculan tablas 2×2, frecuencias, diferencia de proporciones, intervalo descriptivo
con cotas Wilson, sensibilidad leave-one-out y ejemplos/contraejemplos separados
en entrenamiento y holdout. Se exige >=3 observaciones en cada grupo de entrenamiento.
Los segmentos insuficientes quedan registrados, incluidos los jugadores sin serie.

Hay un presupuesto explícito de 64 tests por segmento, repartido entre las tres
categorías. Se registran combinaciones posibles, ejecutadas y no evaluadas. El
orden no usa p-values. No se duplican condiciones binarias complementarias ni
relaciones numéricas propias simétricas. Se guardan TODOS los tests ejecutados,
no solo los hallazgos más atractivos.

Test exacto bilateral de Fisher; corrección global Benjamini–Yekutieli, conservadora
ante dependencia entre hipótesis. Holdout también ajustado globalmente, con
umbrales congelados. Estados:

1. ASSOCIATION_DISCOVERED.
2. DESCRIPTIVELY_SUPPORTED_ASSOCIATION si q entrenamiento <=0,05.
3. TEMPORALLY_VALIDATED_ASSOCIATION solo con q holdout <=0,05, >=3 por grupo y
   dirección concordante.

`football_hypothesis_status=PENDING_EXPLANATION_WITH_SOURCES` se conserva en todos
los estados. Autocorrelación futbolística, variables anidadas y dependencia entre
equipos limitan la interpretación inferencial: ni siquiera un test positivo prueba
causalidad, calibración predictiva ni ventaja comercial. No se estima probabilidad
del próximo partido. El test sintético fuerte supera los gates; el ruido y las
constantes no se promocionan. No se reducen umbrales para conseguir hallazgos reales.

## Resultados reales offline

Salidas finales: `outputs/advanced_discovery_v2/phase3_release/{20261108,20261109}/`.

| Snapshot | Observaciones equipo | Observaciones jugador | OWN | CROSSED | CONDITIONAL | Total asociaciones |
|---|---:|---:|---:|---:|---:|---:|
| 8 noviembre | 10472 | 48 | 373 | 297 | 1180 | 1850 |
| 9 noviembre | 1587 | 0 | 43 | 58 | 147 | 248 |

**Todas son ASSOCIATION_DISCOVERED; cero respaldadas tras ajuste y cero validadas
fuera de muestra.** El motor no confunde p-value pequeño sin ajustar con confirmación.

Ejemplos de relaciones reales propuestas el día 9:

* **Montana (team 1424)**: intervalo entre encuentros observados >5,9167 días,
  frente a goles propios >1. Entrenamiento 4/4 frente a 0/5; p=0,0079365,
  q=1. Evidencia: 1571490, 1571499, 1571510, 1571533. Holdout 2/3 frente a 0/2;
  contraejemplo 1571568. No significa que el descanso real cause más goles:
  el calendario no está completo y hay posible confusión por rival/localía.
* **Ind. Yumbo**: último valor observado de goles >1 asociado con no volver a
  superar 1 gol: entrenamiento 0/5 frente a 4/6; holdout 0/2 frente a 2/4.
  Fixtures favorables a la asociación negativa: 1549573, 1549609, 1549589,
  1549638, 1549656. p=0,060606; q=1. No demuestra regresión causal ni predice
  el marcador siguiente; referencias y condiciones quedan en el expediente.
* **FBC Melgar W**: conceder >1 gol y marcar >1,5 presenta 0/5 frente a 7/9 en
  entrenamiento (p=0,020979; q=1). El holdout no contiene partidos que satisfagan
  la condición y no confirma la relación. El estado final del histórico puede
  explicar la asociación retrospectiva; no es una condición conocida antes de jugar.

Estos ejemplos superan la mera comparación de promedios: usan condiciones,
frecuencias, recurrencia, referencia y contraejemplos cronológicos. Siguen siendo
hipótesis investigables, no señales predictivas demostradas.

## Cobertura y límites concretos

| Métrica / dimensión | Día 8 | Día 9 |
|---|---:|---:|
| Goles propios | 10472/10472 | 1587/1587 |
| Remates propios | 1520/10472 | 157/1587 |
| Tiros a puerta propios | 1539/10472 | 161/1587 |
| Córners propios | 1539/10472 | 161/1587 |
| xG propio | 1272/10472 | 74/1587 |
| Posesión | 41/10472 | 0/1587 |
| Pases | 40/10472 | 0/1587 |
| Goles H1/H2 | 211/10472 | 0/1587 |
| Eventos disponibles | 8/10472 | 0/1587 |
| Formación documentada | 4/10472 | 0/1587 |

Los 48 jugadores del día 8 tienen una observación cada uno: insuficiente para una
serie individual. El día 9 no tiene fixtures individuales verificables en la caché
consultada. `coverage.json` registra denominadores por competición/temporada/género
y `historical_observations.json.audit` conserva motivos de exclusión. Los detalles
de equipo se concentran en partidos recientes: la muestra de entrenamiento de
remates/córners suele ser insuficiente aunque exista el dato en holdout.

Presión, sectores/duelos directos, clima, entrenador, carga total de minutos y
marcador por intervalos no se inventan a partir de posesión o agregados. Goles por
mitad no habilitan modelos de tiros por mitad. Cobertura comercial de mercados
no se consulta: todos los patrones se conservan sin depender de Bwin.

## Archivos, investigación y compatibilidad

Cada ejecución genera:

* `historical_observations.json`: base, IDs, períodos, métricas, fuentes/SHA, eventos,
  roles, fuentes de variables lag/rival y exclusiones.
* `coverage.json`: cobertura real y límites de interpretación.
* `advanced_patterns.json`: todos los tests, presupuestos, evidencia/contraevidencia,
  incertidumbre, validación y aplicabilidad al próximo rival.
* `research_dossiers.json`: estadísticas originales, IDs históricos, participantes
  acreditados, fuentes fechadas/SHA, preguntas, invalidadores y contexto del rival.
* `manifest.json`: corte efectivo, SHA de entrada/código/dependencias y salidas.

Los expedientes son un contrato específico del descubridor, no se afirman validados
contra el schema draft general de Fase 1. El adaptador comercial describe métrica,
período y estado pendiente, con IDs de próximo fixture en applicability. No se envían
automáticamente al mapper V1 ni se renombran sus estados para fabricar elegibilidad.
Jugadores necesitarán exposición/roles/once; cualquier asociación requerirá mercado,
línea, precio, liquidación e identidad con el contrato comercial existente.

LLM manual: seleccionar una hipótesis, entregar su expediente y los registros
referenciados; investigar mecanismo, entrenador/jugadores, rivales comparables y
contraevidencia con fuentes fechadas anteriores al corte del objetivo. Una explicación
no transforma q-values ni tasas históricas en probabilidades propias. Propuestas nuevas
de LLM exigirán una siguiente ejecución Python con entrenamiento/test separados.

## Comandos reproducibles

Desde `/Users/antonio/Projects/scraper`:

```bash
.venv/bin/python prematch_advanced_discovery_v2.py \
  --preselection outputs/discovery/2026-11-09_1500_0300/prospective_trial/enriched/preselection.json \
  --cache-root outputs/.api_football_history_cache \
  --max-tests-per-segment 64 \
  --output-dir outputs/advanced_discovery_v2/phase3_release/20261109

.venv/bin/python prematch_advanced_discovery_v2.py \
  --preselection outputs/discovery/2026-11-08_1500_0300/prospective_trial/recovery_partial/enriched/preselection.json \
  --cache-root outputs/.api_football_history_cache \
  --max-tests-per-segment 64 \
  --output-dir outputs/advanced_discovery_v2/phase3_release/20261108
```

Sin `--cache-root` analiza solo el snapshot. `--allow-retrospective-cache` permite
recuperar crudos históricos adquiridos después del corte, explícitamente etiquetados
RETROSPECTIVE_RECONSTRUCTION y jamás supuestos disponibles entonces. No se usó
esa opción en las dos ejecuciones finales. No se admite LIVE ni fixture posterior
al corte. La política write-once rechaza cambios de entrada/configuración en una
carpeta existente; usar carpeta nueva para otra versión.

## Pruebas y cierre

270 pruebas PASS: las regresiones existentes, integridad individual y 11 nuevas.
Cubren Fisher/BY, ruido, condiciones nuevas con contraejemplos, holdout cronológico,
umbrales independientes del futuro, competición/categoría/jugador separados,
duplicados, null/cero, identidad temporal, caché embebida, reconstrucción explícita,
referencia rival anterior a cada partido, SHA/write-once, idempotencia y compatibilidad
de observaciones V1. Dos integraciones reales repetidas con `socket.connect`
bloqueado; mismos hashes, sin modificación de preselecciones ni Predictors congelados.

```bash
.venv/bin/python -m unittest -q test_prematch_advanced_discovery_v2.py \
  test_prematch_v2_phase2.py test_prematch_v2_integration.py \
  test_prematch_candidate_selector_v1.py test_prematch_predictive_evidence_ranking_v1.py \
  test_prematch_market_mapping_v1.py test_prematch_predictive_engine.py \
  test_prematch_prospective_trial.py test_prematch_daily.py test_prematch_phase1.py \
  test_prematch_phase2.py test_prematch_phase2_player_integrity.py \
  test_prematch_phase3.py test_prematch_pattern_discovery_v2.py \
  test_prematch_tactical_signal_engine.py test_prematch_temporal_windows.py \
  test_prematch_predictive_temporal_guard.py
```

Siguiente acción manual: revisar cobertura y expedientes; priorizar contraejemplos
y variables omitidas antes de encargar investigación cualitativa. Para ampliar
rendimiento individual hace falta una adquisición histórica autorizada y acotada
de identidades, players y XI previos al corte, no una explicación LLM que sustituya
datos ausentes. Falta validar mecanismos, reconstruir estados de marcador con
integridad suficiente, duelos individuales acreditados y modelos nacionales
específicos. No se declaran implementadas esas capacidades.
