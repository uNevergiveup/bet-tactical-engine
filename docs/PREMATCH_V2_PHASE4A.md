# Fase 4A — recuperación histórica offline y adquisición manual

## Diagnóstico corregido

La auditoría de Fase 3 consultaba únicamente `.api_football_history_cache`.
Los 1587 registros del día 9 son **1475 fixtures únicos**, todos presentes en
`outputs/data/cache/preselect_api` como cuerpos de fixture dentro de calendarios
históricos. No faltaban esas identidades en el repositorio: faltaba integrar el
índice legacy. Se contrastan IDs, localía, rival, liga, temporada, kickoff y estado
final. Una versión NS anterior y otra FT posterior no son contradicción de identidad.

| Snapshot | Fixtures únicos | Identidades reutilizadas | Conflictos aislados | Ausentes sin candidato |
|---|---:|---:|---:|---:|
| 9 noviembre | 1475 | 1475 | 0 | 0 |
| 8 noviembre | 8887 | 8858 | 29 | 0 |

Los 29 conflictos del día 8 se mantienen en cuarentena, con cuerpos y fuentes de
evidencia en `conflicting_evidence`; no se fuerza el ID ni se consulta para resolverlos.
Las filas de calendario sin detalle no acreditan estadísticas, jugadores ni XI.

`preselect_matches.py` selecciona `select_detail_games(..., detail_limit)` y obtiene
solo esos detalles en lotes de 20 mediante `/fixtures?ids=...`. El calendario amplio
y los detalles recientes tienen coberturas distintas. Esa adquisición selectiva
explica la concentración de remates/córners en holdout. No prueba que el proveedor
carezca de estadísticas antiguas. La segmentación completa por competición,
temporada, género, equipo y métrica está en `coverage_before` del plan.

## Implementación

`prematch_history_recovery_v2.py` añade un recuperador; conserva descubridor,
Predictor, umbrales y snapshots originales. El cliente ONLINE hereda
Phase1ApiClient: FOOTBALL_API_KEY, caché, presupuesto estricto, errores, reintentos
hasta tres y respuesta 429. Las identidades se buscan en calendarios legacy y
caché compartida; endpoints individuales legacy solo se aceptan bajo el hash
exacto de la consulta. No se adivina el fixture de una respuesta de estadísticas.

La importación escribe envelopes derivados nuevos con fuente original/SHA y
fecha real. Los registros se verifican por contenido; no se inventa un payload API.
Las capturas ONLINE se archivan por SHA antes de importarse. La caché operacional
puede expirar, pero no modifica esos archivos de evidencia archivados.

Estados distintos: CACHE_USABLE, CACHE_PARTIAL_USABLE, CACHED_EMPTY_OR_UNUSABLE,
NOT_QUERIED_IN_KNOWN_CACHE, CACHE_IDENTITY_CONFLICT. Los vacíos no significan
indisponibilidad definitiva. Respuestas sin métricas útiles o parciales tienen TTL
explícito de 300 segundos en la caché nueva del recuperador: no bloquean 30 días ni
provocan reintentos inmediatos idénticos. No se cambia el cliente compartido existente.

Los originales importados se conservan aunque hayan sido adquiridos después del
corte: se identifican como reconstrucción retrospectiva. El descubridor posterior
debe utilizar `--allow-retrospective-cache` explícitamente. Jamás se afirma que la
adquisición actual existía antes del cutoff. Fechas históricas y disponibilidad
observada del resultado permanecen separadas de adquisición/corte.

## Prioridad y presupuesto

La ordenación prima los históricos del tramo de entrenamiento y perspectivas
compartidas, con evidencia estadística propia conocida como criterio secundario.
No filtra ligas menores ni selecciones. No asigna prioridad a jugadores ficticios:
sin una serie identificada, la primera recuperación sirve para determinar quién
tiene muestra. Bwin no se consulta; estas son familias estadísticas potencialmente
compatibles, no mercados comercialmente acreditados para cada fixture.

Se aprovecha `/fixtures?ids=...` en grupos de hasta 20, como hace el proyecto.
Los campos embebidos se aprovechan si realmente están presentes. Las llamadas
individuales posteriores se omiten cuando el lote ya devolvió detalle suficiente.
No se presupone agrupación de `/fixtures/statistics`, players o lineups.

| Snapshot | Lotes de detalle previstos | Solicitudes individuales condicionales | Techo sin reintentos |
|---|---:|---:|---:|
| Día 9 | 74 | 4421 | 4495 |
| Día 8 | 441 | 26450 | 26891 |

Son techos de completar **todo** el universo elegible, no presupuesto recomendado
ni autorización para gastarlo. Hasta tres intentos por petición; el límite global
incluye los reintentos. El piloto recomendado es **10 llamadas nuevas**. Los lotes
pueden aportar ya el detalle y reducir las solicitudes condicionales; puede haber
vacíos o falta de cobertura. No se garantiza una muestra individual utilizable.

## Resultados OFFLINE

Salidas: `outputs/history_recovery_v2/release/{20261108,20261109}/`:

* `recovery_identity.json`: contrato fuente/cutoff para reanudación.
* `plan_<hash>.json`: fixtures, prioridades, evidencias, endpoints, métricas antes,
  conflictos y estimación por tipo de consulta.
* `result_<hash>.json`: llamadas, errores/pendientes, métricas después por segmento,
  observaciones individuales incorporadas y auditoría de consultas.
* `cache/`: importaciones verificadas para el descubridor sin cambiar originales.

Día 9: cero llamadas nuevas; 1475 identidades reutilizadas; 15 observaciones
individuales reconstruibles (no 15 series completas). Remates pasan de 157 a 158
observaciones válidas, córners y tiros a puerta de 161 a 162. El resto sigue pendiente
de adquisición o cobertura, no resuelto mediante imputación. Quedan 1474 fixtures
con al menos un endpoint de detalle incompleto. Día 8: 462 observaciones individuales
recuperables desde archivos existentes. Las cifras por equipo, temporada y familia
están en los planes y `coverage_after_by_segment` de cada resultado.

## Comandos manuales

Desde `/Users/antonio/Projects/scraper`:

```bash
.venv/bin/python prematch_history_recovery_v2.py \
 --preselection outputs/discovery/2026-11-09_1500_0300/prospective_trial/enriched/preselection.json \
 --output-dir outputs/history_recovery_v2/release/20261109
```

ONLINE **no ejecutado**; piloto autorizado al ejecutar el usuario este comando:

```bash
.venv/bin/python prematch_history_recovery_v2.py \
 --preselection outputs/discovery/2026-11-09_1500_0300/prospective_trial/enriched/preselection.json \
 --output-dir outputs/history_recovery_v2/release/20261109 \
 --online --resume --max-api-calls 10
```

Repetir ese comando reanuda con hasta diez llamadas nuevas por ejecución, comprobando
evidencia real antes de reutilizar. No se omiten pendientes por un simple success flag.
Fuente/cutoff incompatibles bloquean la reanudación. Un nuevo snapshot requiere
otro directorio.

Después de revisar el resultado y sus registros adquiridos:

```bash
.venv/bin/python prematch_advanced_discovery_v2.py \
 --preselection outputs/discovery/2026-11-09_1500_0300/prospective_trial/enriched/preselection.json \
 --cache-root outputs/history_recovery_v2/release/20261109/cache \
 --allow-retrospective-cache \
 --output-dir outputs/history_recovery_v2/release/20261109/discovery_new_version
```

Para una prueba prospectiva nueva, utilizar su propio snapshot y cutoff; no trasladar
la etiqueta de disponibilidad histórica de esta reconstrucción a un backtest.

## Pruebas y límites

276 tests PASS: regresiones completas relevantes de Fases 1–3, predictor, mapping,
selector, ventanas, integridad individual y seis nuevas del recuperador. Tests nuevos
simulan transportes: presupuesto/reanudación, identidad legacy sin red, estado NS→FT,
localía incorrecta, agrupación, fecha retrospectiva, vacío/cache/expiración/recuperación,
HTTP 503 y presupuesto agotado. No se hizo una adquisición ONLINE real.
Los modos OFFLINE se probaron con sockets bloqueados y salidas idempotentes.

No se acredita cobertura contratada ni se corrigen por código datos que la API no
ofrezca. Los conflictos de identidad no se convierten en solicitudes ciegas; requieren
revisión de evidencia. Aún no se declara recuperada una serie ni suficientes datos
para significación: esa comprobación corresponde al descubridor tras la adquisición.
