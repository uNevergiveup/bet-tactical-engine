# Fase 4B.1 — revisión metodológica y preparación táctica

Entrada auditada: informe `interpretation_pilot_4b.md` y los cuatro artefactos de
`outputs/history_recovery_v2/player_directed_20261109/discovery_new_version/`.
Salida separada: `outputs/history_recovery_v2/player_directed_20261109/methodology_4b1_release/`.
No hubo adquisición ni servicios externos. No se alteraron originales ni criterios
de significación. Esta ejecución revisa etiquetas y expedientes congelados; no refita estadísticas.

## Causas raíz y correcciones

### Período

`normalize()` añade `own.goals_h1`/`own.goals_h2` a una fila histórica FULL_TIME
cuando existe score.halftime/fulltime consistente. Es razonable que esa fila
represente un partido completo, pero `discover()` copiaba `key[7]` de la fila tanto
a pattern.period como a commercial_adapter.period. Así una métrica H1 heredaba FT.

Ahora `metric_period()` determina el período del resultado: FIRST_HALF/SECOND_HALF
para los sufijos explícitos y FT para los demás. `observation_period` conserva FT;
`condition_period` distingue el período de la condición, y el adaptador lleva
métrica goals y período H1/H2 correspondiente. No se cambia la serie ni se transforma
el umbral. El patrón lógico mantiene su ID; metodología 2.1 y manifiesto padre
hacen explícita la revisión. Los originales de 4B conservan su etiqueta original.

### Holdout

La comprobación antigua `same_direction` no explicaba por qué era falsa y los
expedientes no distinguían n=0 de efecto cero. La validación estadística existente
ya requería grupos mínimos y q favorable; no se encontró una aprobación positiva
en estos cinco casos. Se añade `holdout_assessment` con categorías excluyentes:

* NO_VALID_OBSERVATIONS: ninguna fila válida.
* NO_COMPARABLE_REFERENCE_GROUP: falta uno de los grupos.
* NO_OUTCOME_VARIATION: el resultado binarizado no varía.
* INSUFFICIENT_COMPARABLE_SAMPLE: grupos presentes pero alguno con n<3.
* CONTRARY_EFFECT / CONSISTENT_EFFECT / NO_OBSERVED_EFFECT: comparación posible
  y soporte mínimo; la dirección se describe independientemente.

`comparison_possible`, soporte por grupo y `predictive_validation=false` se
mantienen separados. Un efecto consistente no demuestra significación, calibración
ni capacidad predictiva. Las tablas, p-values y q-values permanecen intactas.

### Por 90 minutos

La normalización ya conserva minutos positivos, totales y tasas, y solo incorpora
tasas al descubrimiento con >=30 minutos observados. No se cambió ese mínimo.
La nueva auditoría verifica `90 * conteo / minutos` por fixture y explicita métrica,
unidad, registros observados, n válido, minutos elegibles, titularidad y posición.
Un denominador/tasa incoherente mantiene bloqueado el uso predictivo. No se suman
minutos de métricas distintas ni se presenta la tasa media de partidos como tasa
agrupada. Los totales originales tampoco se sustituyen por per90.

Se señalan exposiciones <60 minutos para revisión y se calcula una sensibilidad
excluyéndolas **con el umbral original fijo**, sin cambiar el test principal ni
el entrenamiento/holdout. Es un diagnóstico de exposición, no un nuevo criterio
de significación ni una búsqueda de resultados favorables.

Ramírez: entrenamiento 8 registros, 606 minutos; holdout 4, 360 minutos. Todas las
fórmulas son verificables. El entrenamiento incluye 1519381 con 45 minutos y tasa
90 pases/90: no son 90 pases realmente completados ni garantía de exposición futura.
Los restantes registros locales también superan el umbral congelado, por lo que
el partido corto no explica por sí solo la separación. Se conserva confusión
por posición nominal M, rival, posesión y titularidad no acreditada en un histórico.
Jiménez: su patrón seleccionado usa conteos de faltas, no per90; el suplente de
25 minutos del holdout se señala en el informe previo, sin inventarle otra exposición.

## Cinco expedientes: antes/después

| Patrón (prefijo ID) | Antes | Después |
|---|---|---|
| b8888b686d42, Ramírez | same_direction=false | NO_OUTCOME_VARIATION; auditoría per90 y sensibilidad por exposición |
| aa6c3ab05336, Jiménez | same_direction=false | NO_OUTCOME_VARIATION; conteos y minutos no confundidos |
| 138f4a156317, FC Koln W | same_direction=false | NO_VALID_OBSERVATIONS (holdout 0/3 válido) |
| 8ec3557a7001, Manta/córners | same_direction=false | NO_COMPARABLE_REFERENCE_GROUP; holdout solo 4-4-2 |
| a3243f3d80b2, Union Magdalena | FT y resultado H1 | FIRST_HALF; CONTRARY_EFFECT, +0,8333 → −0,3333 |

`comparison.json` verifica igualdad exacta antes/después de tablas, variables,
umbrales, validación y p/q en los cinco casos. Todos conservan q=1 y autorización
predictiva falsa. No se crea ninguna explicación documentada ni validación nueva.

Los expedientes nuevos incluyen preguntas generadas con IDs de jugador/equipo,
variable y umbral, rival objetivo, fixtures favorables/contrarios, estado concreto
del holdout y condiciones de refutación. Los casos per90 piden revisar los fixtures
de menor exposición; H1/H2 exige evidencia del período correcto. Siguen siendo
preguntas para fuentes fechadas, no respuestas LLM inventadas.

`knowledge_stages` separa statistical_finding, llm_hypothesis, sourced_explanation
y predictive_pattern. mechanism permanece null y explanation_status permanece
PENDING_EXTERNAL_EVIDENCE. El texto del piloto humano es una propuesta interpretativa,
no una fuente que complete esos campos de hechos.

## Ampliación histórica diversificada

`diversification_proposal()` en el recuperador existente genera una propuesta de
equipos y fixtures antiguos con huecos de entrenamiento. Primera pasada: una
competición por fila; después, restantes equipos. Considera evidencia local de
familias Bwin observadas y base individual ya adquirida, sin descartar otras ligas.

Solo se acredita presencia de familia **en la captura del evento**. No se afirma que
exista una selección para cada equipo/jugador ni que el precio esté vigente. Se
comprueba SHA del TXT y join; no se transfieren mercados a otra competición o temporada
del mismo equipo. Los 5 preflights usados son los originales del día 9. Las
selecciones concretas y disponibilidad individual requieren auditoría comercial posterior.

| Prioridad | Equipo API | Competición | Primeros fixtures propuestos | Evidencia de mercado observada |
|---:|---|---:|---|---|
|1|Sportivo Luqueno, 1183|252|1584340, 1584345, 1610933|Familias de goles en evento capturado|
|2|Aurora, 3637|344|1618976, 1618983, 1618993|Familias de goles en evento capturado|
|3|FC Botosani, 2581|283|1565186, 1565196, 1565205|Familias de goles en evento capturado|
|4|Junior, 1135|239|1546413, 1549687, 1549701|Goles, córners y goleadores en evento capturado|
|5|Mushuc Runa SC, 1162|242|1519382, 1519391, 1519398|No acreditada en los preflights suministrados|
|6|FC Koln W, 1871|82|1401341|No acreditada en los preflights suministrados|
|7|FC Iberia 1999, 3502|848|1636471|No acreditada en los preflights suministrados|
|8|Bari, 508|891|1611309|No acreditada en los preflights suministrados|

El ranking completo contiene 192 segmentos de equipo; no son 192 equipos únicos.
`acquisition_proposal.json` enumera todos los fixtures, endpoints players/lineups/
statistics, límites y coste máximo orientativo de tres llamadas por fixture antes
de reutilizar caché. Es una **propuesta**, no una cola autorizada de adquisición.
Los cuerpos de fixture, caché disponible, exposición y oferta real deben revalidarse
antes del siguiente piloto. No se fuerza conseguir más significación ni se promete
que participaran los jugadores conocidos.

## Ejecución manual reproducible

Desde `/Users/antonio/Projects/scraper`, solo reproceso OFFLINE:

```bash
.venv/bin/python prematch_advanced_discovery_v2.py \
 --reprocess-frozen-dir outputs/history_recovery_v2/player_directed_20261109/discovery_new_version \
 --pattern-id b8888b686d42 --pattern-id aa6c3ab05336 --pattern-id 138f4a156317 \
 --pattern-id 8ec3557a7001 --pattern-id a3243f3d80b2 \
 --preflight outputs/discovery/2026-11-09_1500_0300/prospective_trial/daily_runs/20261109T020353042168/preflight/1632060_0/market_preflight.json \
 --preflight outputs/discovery/2026-11-09_1500_0300/prospective_trial/daily_runs/20261109T020353042168/preflight/1645541_0/market_preflight.json \
 --preflight outputs/discovery/2026-11-09_1500_0300/prospective_trial/daily_runs/20261109T020353042168/preflight/1565299_0/market_preflight.json \
 --preflight outputs/discovery/2026-11-09_1500_0300/prospective_trial/daily_runs/20261109T020353042168/preflight/1549860_0/market_preflight.json \
 --preflight outputs/discovery/2026-11-09_1500_0300/prospective_trial/daily_runs/20261109T020353042168/preflight/1632059_0/market_preflight.json \
 --output-dir outputs/history_recovery_v2/player_directed_20261109/methodology_4b1_release
```

Siguiente acción: revisar la propuesta y fijar un subconjunto multicompetición y
presupuesto del próximo piloto; no ejecutar el viejo plan de Manta esperando que
implemente esta diversificación. El puente de interpretación a una cola comercial
o LLM completa no se ha implementado.

## Pruebas

282 pruebas PASS en la regresión completa relevante y nuevas pruebas metodológicas: período sin refit,
holdout ausente/sin referencia/sin variación/contrario/consistente, minutos/tasas,
sensibilidad y denominador inválido bloqueado. Integración con sockets bloqueados,
cinco patrones únicos, originales comprobados por SHA e idempotencia del reproceso.
No se modificaron datos históricos ni se ejecutaron servicios externos.
