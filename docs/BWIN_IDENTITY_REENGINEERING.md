# Reingeniería API-Football ↔ Bwin Event ID

## Alcance y decisión

Esta implementación modifica exclusivamente descubrimiento, resolución y trazabilidad de identidad. No cambia modelos, umbrales comerciales, picks, cuotas ni investigación cualitativa. La validación y las mediciones descritas aquí son offline; no se consultó API-Football ni Bwin.

## Causa raíz demostrada

La evaluación `20261113T140059021954Z_c0b4ec5b12d1` dividió 120 fixtures en 24 lotes de cinco. Cada lote volvió a recorrer el catálogo Bwin. Después, `prematch_runtime.discovery_for()` lanzó 70 descubrimientos por fixture no resuelto. El artefacto consolidado acumuló 8.931 filas para solo 450 Event IDs únicos y descartó el detalle de páginas al consolidar.

Por tanto, `NOT_FOUND_IN_SCANNED_PAGES` representaba cuatro problemas distintos:

1. catálogo parcial o sin prueba de agotamiento;
2. candidato no recuperado por idioma/alias;
3. candidato recuperado pero sin competición/categoría/orden/tiempo corroborados;
4. ausencia en el catálogo observado.

No acreditaba que Bwin no ofreciese el partido. La repetición provenía de dos bucles: adquisición por lote en `prematch_prospective_trial.py` y reintento completo por fixture en `prematch_runtime.py`.

## Arquitectura implementada

1. La preparación diaria realiza una sola adquisición compartida para todos los fixtures asignados al presupuesto.
2. El catálogo canónico se deduplica por Event ID y conserva URL, texto original, fuente, páginas, timestamp, alcance, hash del snapshot y campos estructurados solo cuando son observables.
3. El matching se ejecuta localmente sobre ese índice. Los aliases solo recuperan candidatos; nunca confirman identidad.
4. La confirmación exige equipos, orden local/visitante, competición, categoría y tiempo. Un dato ausente conserva el bloqueo.
5. El auditor recorre API → Bwin y Bwin → API. Los eventos Bwin no vinculados no desaparecen y conservan posibles fixtures API.
6. Tras la adquisición compartida, el flujo diario ya no reabre el catálogo completo para cada fixture. Una búsqueda dirigida posterior queda como operación separada, acotada y trazable.
7. El handoff diario referencia el informe de cobertura con SHA-256; el expediente canónico conserva esa referencia sin convertir candidatos ambiguos en enlaces accionables.

## Estados

- `VERIFIED_LINK`: equipos, orden, competición, categoría y kickoff corroborados.
- `AMBIGUOUS_LINK`: más de una alternativa plausible dentro del margen configurado.
- `NAME_MISMATCH`: existe candidato, pero nombres, orden o competición no alcanzan el contrato.
- `TIME_CONFLICT`: diferencia de kickoff superior a cinco minutos.
- `CATEGORY_CONFLICT`: género o categoría incompatibles.
- `NOT_FOUND_IN_CATALOG`: solo puede usarse cuando el alcance capturado acredita terminación.
- `SEARCH_INCOMPLETE`: catálogo incompleto o evidencia obligatoria ausente.
- `NOT_OFFERED_VERIFIED`: reservado; no se emite sin evidencia comercial independiente.
- `SOURCE_UNAVAILABLE`: la fuente no pudo auditarse.
- `NOT_SEARCHED_BUDGET`: no se ejecutó la búsqueda por presupuesto.

Los umbrales del auditor son 0,72 para recuperación amplia, 0,82 para compatibilidad semántica, 0,94 para nombres de un enlace verificable, 0,82 para competición, margen de ambigüedad 0,05 y tolerancia temporal de cinco minutos. Ningún umbral sustituye las contradicciones duras.

## Normalización y aliases

`config/bwin_team_aliases.json` es un registro auditable con política `CANDIDATE_RETRIEVAL_ONLY_NEVER_IDENTITY_CONFIRMATION`. Incluye acentos, traducciones de selecciones, abreviaturas e identidades institucionales acreditadas. No elimina sufijos de género/juvenil de forma global ni convierte coincidencias parciales en identidad.

La comparación elimina del texto de nombres los relojes `Hoy/Mañana` y fechas para impedir coincidencias accidentales con clubes numerados. También elimina el sufijo técnico `-2` de la ruta Bwin. El orden puede corroborarse con variantes multilingües, pero sigue sujeto a todas las demás señales.

## Auditoría de la ventana interrumpida

Se publican dos vistas complementarias:

- Inventario API completo, 154 fixtures: `outputs/audits/bwin_identity_20261113_full_v2/`.
- Inventario enriquecido realmente sometido a Bwin, 120 fixtures: `outputs/audits/bwin_identity_20261113_enriched_v2/`.

El inventario bruto de 154 no conserva género/categoría explícitos: 154 quedan con categoría desconocida. Con el contrato estricto produce 0 enlaces verificados, 148 búsquedas incompletas, 5 conflictos de categoría y 1 discrepancia de nombre. Esto es una limitación del artefacto de entrada, no una ausencia de Bwin.

En los 120 fixtures enriquecidos hay 13 con categoría adulta explícita y 107 con categoría todavía desconocida. El resultado es:

| Estado | Total |
|---|---:|
| `VERIFIED_LINK` | 4 |
| `SEARCH_INCOMPLETE` | 109 |
| `NAME_MISMATCH` | 7 |
| restantes | 0 |

Los cuatro enlaces reproducen Internacional de Bogotá–Santa Fe, Sporting San Miguelito–Árabe Unido, Lexington–Fort Lauderdale (F) y UMECIT–Plaza Amador. Jezero–Buducnost, aceptado por el flujo anterior, queda correctamente bloqueado porque `First League` y `1.CFL` no están corroborados como equivalentes por la evidencia congelada.

Casos señalados:

- Fixture `1528965`, Serbia–Alemania: `SEARCH_INCOMPLETE`, sin candidato de dos equipos en las páginas observadas. No se afirma ausencia en Bwin.
- Fixture `1528968`, Moldavia–Kazajistán: candidato observado `7909696`, nombres traducidos, orden y hora compatibles; sigue `SEARCH_INCOMPLETE` porque el snapshot de catálogo no acredita competición ni categoría. No se entrega Event ID confirmado.

El snapshot antiguo no retuvo `pages`, por lo que el auditor no declara el catálogo completo y no emite `NOT_FOUND_IN_CATALOG`.

## Dataset de regresión

`config/bwin_identity_regression.json` contiene siete pares reales con Fixture ID, Event ID, fecha, competición, categoría, ruta de evidencia y SHA-256:

- Barcelona–Alavés: `1570434` ↔ `7899092`.
- Juventus Women–OL Lyonnes: `1638311` ↔ `7910157`.
- Deportivo Pasto–Junior: `1549868` ↔ `7912964`.
- Internacional de Bogotá–Santa Fe: `1549869` ↔ `7912957`.
- Sporting San Miguelito–Árabe Unido: `1583807` ↔ `7912642`.
- Lexington–Fort Lauderdale United (F): `1557233` ↔ `7910709`.
- UMECIT–Plaza Amador: `1583809` ↔ `7912640`.

Incluye controles negativos de orden invertido, género, juvenil y homónimo de otro país. La regresión offline obtiene 7/7 verdaderos positivos, 0 falsos positivos, precisión 1,0 y recall 1,0 sobre el denominador explícito de siete presencias acreditadas. Este resultado no se extrapola a todos los fixtures API.

## Rendimiento reproducible

`outputs/audits/bwin_identity_20261113_enriched_v2/benchmark.json` mide los artefactos congelados:

| Métrica | Antes | Nueva arquitectura |
|---|---:|---:|
| adquisiciones completas | 94 | 1 por evaluación |
| lotes iniciales | 24 | 1 |
| reintentos completos por fixture | 70 | 0 |
| navegaciones registradas | 456 | pendiente de piloto autorizado |
| filas consolidadas / IDs únicos | 8.931 / 450 | índice deduplicado |
| suma de duración de adquisiciones antiguas | 3.621,771921 s | no medida online |
| matching local congelado | no aislado | 8,509249 s para 120 × 450 |

La reducción estructural es 95,833333 % frente a los 24 lotes iniciales y 98,93617 % frente a las 94 adquisiciones observadas. No se estima la duración de una nueva captura browser porque no hubo autorización de red.

## Comandos offline

Auditar todos los fixtures sin red:

```bash
.venv/bin/python bwin_identity_audit.py \
  --fixtures outputs/discovery/2026-11-13_1500_0300/evaluations/20261113T140059021954Z_c0b4ec5b12d1/fixtures.json \
  --catalog outputs/discovery/2026-11-13_1500_0300/evaluations/20261113T140059021954Z_c0b4ec5b12d1/prospective_trial/bwin/bwin_discovery.json \
  --output-dir outputs/audits/bwin_identity_20261113_full_v2
```

Consultar no enlazados y candidatos bidireccionales:

```bash
python3 -c 'import csv; p="outputs/audits/bwin_identity_20261113_enriched_v2/identity_audit.csv"; [print(r) for r in csv.DictReader(open(p, encoding="utf-8-sig")) if r["link_status"] != "VERIFIED_LINK"]'
```

Reproducir la regresión real:

```bash
.venv/bin/python bwin_identity_regression.py --output-dir outputs/audits/bwin_identity_regression_v2
```

Reproducir el benchmark:

```bash
.venv/bin/python bwin_identity_benchmark.py \
  --evaluation outputs/discovery/2026-11-13_1500_0300/evaluations/20261113T140059021954Z_c0b4ec5b12d1 \
  --identity-audit outputs/audits/bwin_identity_20261113_enriched_v2/identity_audit.json \
  --output outputs/audits/bwin_identity_20261113_enriched_v2/benchmark.json
```

## Prueba online controlada pendiente de autorización

No ejecutar hasta disponer de autorización expresa. El procedimiento es:

1. generar/congelar un inventario futuro real y anotar un único instante de corte;
2. seleccionar cinco fixtures realmente presentes: club masculino de primera, club femenino de primera, selección absoluta, nombre difícil y uno ausente del catálogo general;
3. ejecutar una sola adquisición compartida con los cinco `--fixture-id` y `--directed-pilot`;
4. permitir como máximo diez páginas entre catálogo general y complementos dirigidos;
5. verificar cada Event ID desde la URL/página del evento, competición, categoría y kickoff;
6. guardar snapshot, páginas, timestamps y hashes; no usar datos posteriores al kickoff;
7. repetir el auditor offline sobre el snapshot congelado y comparar precisión, recall conocido, duración y navegaciones.

Plantilla, una vez que esos IDs reales se hayan elegido:

```bash
.venv/bin/python bwin_discovery.py \
  --input /ruta/al/inventario_futuro_congelado.json \
  --fixture-id ID_1 --fixture-id ID_2 --fixture-id ID_3 --fixture-id ID_4 --fixture-id ID_5 \
  --headless --directed-pilot --bwin-first-max-pages 5 --max-country-pages 5 \
  --output-dir outputs/pilots/bwin_identity/AAAAMMDDTHHMMSSZ
```

## Limitaciones pendientes

- La captura online de la nueva arquitectura y su duración real no se han ejecutado.
- El catálogo DOM no siempre publica competición, categoría u hora absoluta; esos casos requieren inspección del evento, no relajación del contrato.
- 107/120 fixtures del inventario enriquecido carecen de categoría adulta explícita. Debe conservarse esa señal desde preselección para mejorar cobertura verificable.
- El registro de alias requiere evidencia y revisión; no aprende equivalencias globales automáticamente.
- `NOT_OFFERED_VERIFIED` continúa sin uso hasta disponer de evidencia suficiente de ausencia comercial.

