# Objetivo 6 — STATISTICAL_BASELINE_PREMATCH

Ejecución offline sobre la reconciliación del Objetivo 5.1, con 21 identidades verificadas. Se reutilizan observaciones normalizadas y el descubridor avanzado existente. No hubo acceso externo, actualización de precios, alineaciones ni investigación cualitativa. Los expedientes representan el corte congelado de cada predicción; no certifican que el evento siga PREMATCH al leerlos.

## Resultado y criterios

21 fixtures examinados, 21 elegibles: 4 `DATA_READY` y 17 `PARTIAL`. No hay identidades bloqueadas en la cohorte de entrada. `DATA_READY` exige ocho observaciones válidas por equipo, en competición y temporada actuales, para goles, córners, amarillas, faltas, remates y tiros a puerta. Es un criterio de cobertura para investigación, sin modificar reglas comerciales. Un expediente parcial puede contener familias utilizables: se conservan sus datos y limitaciones.

Corrección del Objetivo 5.1: sus 64 observaciones de Nations League eran 16 equipos × 4 antecedentes, un agregado de liga. No representaban 64 antecedentes por partido. La edición actual tiene cuatro observaciones por equipo y queda `PARTIAL`; los históricos de otras temporadas permanecen separados y disponibles. Identidad Bwin verificada no acredita cobertura suficiente.

Los JSON `historical_groups` contienen tamaño real, temporada, competición, equipo, localía, fixture IDs, media, mediana, dispersión, varianza/media, frecuencia positiva y reciente frente a anterior para cada métrica, incluidos BTTS y totales conjuntos observados. No se suman dos veces los dos equipos para construir una muestra de partidos independientes. La ausencia de dato permanece ausente.

## Auditoría de lo existente

Se inspeccionaron `preselection.json`, las 14.517 observaciones congeladas, `advanced_patterns.json`, el descubridor `prematch_advanced_discovery_v2.py`, los patrones phase2 y los modelos de `prematch_commercial_models.py` con su validación real anterior.

En la cohorte seleccionada hay 10.303 patrones originales distintos, conservados íntegros en `original_python_patterns`, con recuentos por métrica/mercado en `original_by_market_metric`. Incluyen numerosos patrones individuales: pases 940, precisión de pases cruda 935, duelos totales 878, duelos ganados 775 y amarillas 688. Esto no equivale a 10.303 modelos útiles: son asociaciones históricas con distinta exposición y calidad. Los patrones originales de fueras de juego quedan solo como inventario histórico y se excluyen de candidatos nuevos.

El descubridor existente usa grupos exactos de entidad, equipo, jugador, competición, temporada, género y periodo; división cronológica 70/30; umbrales medianos ajustados en entrenamiento; Fisher bilateral y Benjamini–Yekutieli; evidencia contraria y soporte por grupo. Las relaciones entre estadísticas del mismo partido son descriptivas y no se convierten en variables conocidas del encuentro objetivo. El presupuesto es 64 pruebas por segmento y su truncamiento queda registrado.

Los modelos comerciales existentes funcionan técnicamente para conteos empíricos con suavizado Jeffreys, totales conjuntos y evaluación secuencial. La auditoría histórica contiene 120 contratos `MODEL_IMPLEMENTED_UNCALIBRATED`, cero selecciones comercialmente validadas y cero valor demostrado. Las liquidaciones de tarjetas, hándicaps, combinaciones e identidades individuales siguen teniendo sus bloqueos específicos. Se preservaron todos esos módulos.

## Descubrimiento Python y validación

Se ejecutó Python sobre la cohorte completa: 1.138 asociaciones, con 427 resultados FT, 360 de primera mitad y 351 de segunda mitad. Los periodos proceden exclusivamente de campos observados congelados; no se calcula una mitad por proporción de FT. Los jugadores requieren posición y condición titular histórica conocida, al menos 30 minutos por observación, cinco partidos y 300 minutos por estrato. La muestra y el presupuesto del descubridor pueden impedir generar patrones incluso con datos disponibles.

Clasificación de las 1.138 asociaciones:

| Estado | Número | Criterio |
|---|---:|---|
| VALIDADO | 0 | No hay asociación con soporte temporal corregido suficiente |
| EXPLORATORIO | 155 | Soporte para estudiar la asociación, sin validación temporal corregida |
| INSUFFICIENT_SAMPLE | 935 | Algún grupo de entrenamiento/prueba tiene menos de tres observaciones |
| DESCARTADO | 48 | La dirección del efecto se invierte en la prueba temporal con grupos suficientes |

Cada patrón contiene intervalos descriptivos, p y q, estabilidad, evidencia favorable, contraejemplos, IDs de entrenamiento y prueba y adaptador de familia/periodo. `ranking.json` añade q global conservador sobre todos los expedientes. No se agrupan significaciones repetidas como corroboraciones independientes. Los dos equipos pueden compartir históricos; el fixture es el bloque de dependencia. No hay validación predictiva ni comercial autorizada.

## Ampliación Codex después de Python

Tras examinar los resultados de `objective6_python_v1`, Codex formuló cuatro hipótesis en `config/objective6_codex_hypotheses.json`: ventana reciente para BTTS, localía para córners, referencia histórica de tiros bloqueados para córners e interacción producción propia/concesión rival para goles. Cada una especifica variable, evidencia de origen, alternativa y refutación.

Python produjo 512 contrastes (128 estratos × 4 hipótesis), conservados incluso cuando no tienen observaciones evaluables. Todos quedaron `INSUFFICIENT_SAMPLE`: máximo de 12 predicciones secuenciales por estrato en las tres primeras hipótesis y de una en la interacción con rival. Se requieren 20 evaluaciones para considerar siquiera el resultado exploratorio evaluable. El candidato se compara con una media/frecuencia expansiva y registra error cuadrático; para BTTS binario ese error es Brier. No se eligieron ventanas mediante optimización sobre prueba.

La observación histórica objetivo nunca entra en el ajuste: solo antecedentes disponibles antes de su kickoff. La hipótesis de tiros bloqueados usa una distribución de antecedentes; no presupone los bloqueos del encuentro futuro. Codex ya vio estos resultados congelados: incluso un contraste favorable requeriría datos nuevos reservados para validación independiente. No se proclama validación con esta reutilización.

## Ranking transversal estadístico

Orden principal: mínimo de cobertura de las seis familias por equipo en competición/temporada actuales, después riesgo estadístico y Fixture ID como desempate. Es una prioridad de investigación transparente, no un score comercial optimizado. `ranking.json` contiene además el orden transversal de patrones por q temporal/global.

| Orden | Fixture ID | Partido | Mínimo por familia/equipo | Estado |
|---:|---:|---|---:|---|
| 1 | 1520977 | Ponte Preta–Operario-PR | 20 | DATA_READY |
| 2 | 1520978 | São Bernardo–Athletic Club | 20 | DATA_READY |
| 3 | 1549868 | Deportivo Pasto–Junior | 18 | DATA_READY |
| 4 | 1549870 | Llaneros–Chico | 18 | DATA_READY |
| 5–12 | 1528958–1528964 y 1545613 | Ocho UEFA Nations League | 4 | PARTIAL |
| 13 | 1638295 | Barcelona W–Arsenal W | 3 | PARTIAL |
| 14 | 1638339 | Benfica W–Austria Wien W | 3 | PARTIAL |
| 15 | 1559703 | Aurora–Mixco | 0 | PARTIAL |
| 16 | 1600658 | Águila–Municipal Limeño | 0 | PARTIAL |
| 17 | 1600659 | Atletico Balboa–Inca Aruba | 0 | PARTIAL |
| 18 | 1644363 | Navalcarnero–Real Betis | 0 | PARTIAL |
| 19 | 1644370 | Calvo Sotelo–Albacete | 0 | PARTIAL |
| 20 | 1644379 | Tarancón–Levante | 0 | PARTIAL |
| 21 | 1649056 | Cerro Largo–Progreso | 0 | PARTIAL |

Un mínimo de cero señala al menos una familia ausente, no todos los datos ausentes. El detalle independiente por equipo está en cada expediente. El riesgo 4/5 indica cobertura suficiente sin asociaciones validadas; 5/5 añade cobertura insuficiente. Es una rúbrica de incertidumbre estadística, no de resultado deportivo.

## Entrega congelada y reproducción

Directorio final: `outputs/control/runs/objective6_STATISTICAL_BASELINE_PREMATCH_v1/`. Contiene 21 pares `<Fixture ID>.json/.md`, `ranking.json`, `summary.json` y `manifest.json`. El manifiesto verifica 463 ficheros de procedencia y hashes de todos los expedientes. Se conservan los intentos anteriores para trazabilidad. Las preguntas para investigación cualitativa están en cada expediente; esa investigación no se ejecutó.

```bash
.venv/bin/python -B prematch_statistical_baseline.py \
  --root outputs/control/runs/python_engine_real_v1 \
  --reconciliation outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json \
  --hypotheses config/objective6_codex_hypotheses.json \
  --output-dir outputs/control/runs/objective6_replay_NEW

.venv/bin/python -m unittest -q test_prematch_statistical_baseline.py \
  test_prematch_advanced_discovery_v2.py test_prematch_commercial_models.py
```

30 pruebas correctas: fuga temporal por disponibilidad, exclusión de la observación objetivo, campos ausentes frente a cero, rival sin datos, soporte insuficiente frente a inversión temporal, además de las regresiones existentes del descubridor y modelos. La escritura exige directorio nuevo y no sobrescribe originales. La nueva ejecución es determinista sobre los mismos inputs; los hashes acreditan qué fuentes se usaron.

Se realizó una segunda ejecución completa en `objective6_reproduction_v1`: sus 44 hashes de artefactos y 463 hashes de procedencia son idénticos al baseline final. Esto prueba reproducción de resultados sobre los inputs congelados, sin atribuir capacidad predictiva al hecho de reproducirlos.
