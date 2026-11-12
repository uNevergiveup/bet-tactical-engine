# PREMATCH: preparación y ejecución bajo control del usuario

## Alcance

La entrada recomendada es `prematch_daily.py prepare|dry-run|run`. Reutiliza Fase 1, la preselección, Fase 2, el motor avanzado, el predictor y el mapeador existentes. No sustituye sus modelos ni modifica los umbrales comerciales. No genera apuestas, stakes ni el informe final de picks.

`prepare` escribe exclusivamente un plan local con hashes. `dry-run` lo lee y comprueba sin iniciar subprocesos ni servicios. `run` es **offline por defecto**. Sólo `run --external --confirm-external HASH_EXACTO` puede activar servicios externos configurados en el plan.

Se detuvo la adquisición activa cuando el usuario cambió el alcance. Después del cambio sólo se desarrollaron interfaces y se ejecutaron pruebas offline con fixtures reproducibles. No se ha ejecutado una jornada operativa mediante la nueva interfaz.

## Comandos manuales

Desde la raíz del repositorio, preparar una jornada nueva (ejemplo: modificar la fecha cuando corresponda):

```sh
.venv/bin/python -B prematch_daily.py prepare \
  --date 2026-11-12 --start 15:00 --end 03:00 \
  --history 20 --api-budget 1000 --bwin-events 4 \
  --llm-max-iterations 3 \
  --plan outputs/control/plans/jornada_manual_01.json \
  --output-dir outputs/control/runs/jornada_manual_01

.venv/bin/python -B prematch_daily.py dry-run \
  --plan outputs/control/plans/jornada_manual_01.json
```

Estos dos comandos no adquieren datos. El segundo muestra el hash de aprobación, los presupuestos y el estado del runtime. Para usar un Chromium instalado puede añadirse `--browser-executable '/ruta/al/ejecutable'` en `prepare`. Una ruta inexistente se rechaza antes de ejecutar.

Sólo cuando el usuario decida consumir servicios, debe copiar el SHA256 completo impreso:

```sh
.venv/bin/python -B prematch_daily.py run \
  --plan outputs/control/plans/jornada_manual_01.json \
  --external --confirm-external 'SHA256_COMPLETO_IMPRESO_POR_PREPARE'
```

La cadena del ejemplo no autoriza nada: debe sustituirse por el hash real del plan. Cambiar código, configuración o entradas vinculadas obliga a preparar otro plan. El presupuesto API incluye reintentos y es un máximo, no una orden de agotarlo ni una garantía de cuota diaria disponible. `--bwin-events` limita los fixtures inspeccionados, no el número de peticiones HTTP de una página dinámica; cero desactiva la adquisición Bwin. Las capturas profundas sólo se intentan para asociaciones verificadas.

Para una ejecución totalmente local, preparar un plan con `--api-budget 0 --bwin-events 0`, `--fixtures-input RUTA_CANONICA`, y opcionalmente `--bwin-input CATALOGO_LOCAL` y `--preflight PREFLIGHT_LOCAL` repetible. Después:

```sh
.venv/bin/python -B prematch_daily.py run --plan RUTA_DEL_PLAN_LOCAL
.venv/bin/python -B prematch_daily.py run --plan RUTA_DEL_PLAN_LOCAL --resume
```

No se sustituye una jornada por otra. Una entrada cuyos fixtures quedan fuera de la ventana planificada se rechaza. El límite final inclusivo se conserva cuando lo declara el inventario, incluido el encuentro de las 03:00 del inventario del 12/11.

## Reanudación y conservación

Cada resultado pertenece a un plan y carpeta propios. El controlador mantiene un bloqueo de proceso, un estado y un registro de eventos. Verifica los hashes de las etapas terminadas antes de reutilizarlas. Los logs de nuevos intentos tienen nombres distintos.

Se reserva presupuesto **antes** de iniciar un servicio. Se libera la parte no gastada sólo tras recibir un resumen de consumo completo. Si el proceso API o LLM termina sin resumen fiable, la reserva permanece consumida a efectos del límite: `--resume` no crea presupuesto nuevo. En ese caso hay que revisar el diario y preparar explícitamente otro plan/carpeta si se necesita ampliar o reconstruir la etapa incierta. No hay reintentos externos ilimitados ni recuperación silenciosa de saldo.

Una reanudación conserva el corte y las entradas del mismo análisis. Actualizar cuotas, calendario o contexto constituye una nueva captura/plan; no convierte un resultado congelado en información actual. Un resultado `RLM_PENDING` ya archivado tampoco se sobrescribe para fingir una investigación posterior.

El cliente API publica versiones nuevas de una caché caducada mediante creación exclusiva y conserva el archivo anterior. Tanto adquisición como lectores estadísticos admiten estas versiones. Cada intento HTTP se registra en `api_requests.jsonl` antes del envío, seguido de la respuesta y del hash del snapshot confirmado. No se guardan credenciales en este diario. Una reserva de intento no prueba por sí sola que el proveedor lo haya facturado.

## Motores y artefactos

El flujo manual conecta:

1. Fase 1 con presupuesto duro, todas las competiciones y caché compartida.
2. Preselección V1.3 desde los crudos verificados; conserva todos los fixtures y las fórmulas existentes de cobertura.
3. Patrones colectivos e individuales, exposición por métrica, formación y entrenador históricos.
4. Descubrimiento avanzado y validación temporal, sin convertir asociaciones en probabilidades.
5. Ranking de investigación completo y cruce con mercados Bwin verificados o pendientes.
6. Investigación LLM real, si se configura y autoriza, o dependencia pendiente explícita.
7. Explicaciones por familia y encuentro en `explanations.json` y `explanations.md`.

Además de los artefactos existentes, produce:

- `phase2/player_duels_<fixture>.json`: faltas provocadas frente a cometidas, perfiles separados por equipo, jugador, competición, temporada y posición nominal; muestras, minutos y procedencia. Una copresencia histórica no acredita un duelo directo. XI y roles del objetivo siguen pendientes.
- `advanced/historical_observations.json`: métricas normalizadas y referencias a los snapshots.
- `llm/llm_pattern_hypotheses.json`, `pattern_validation.json`, `pattern_registry.json`, `ranking_enriched.json`.
- `llm/runtime_dependency.json` cuando no se ejecuta un runtime.
- `market_mapping/market_mapping.json`, ranking completo y explicaciones.

Las estadísticas por mitad provienen exclusivamente de `statistics_1h`, `statistics_2h` o marcadores oficiales por periodo. No se reparte FT para fabricar córners, faltas o remates de una mitad. Los registros de jugadores AET/PEN no se mezclan con los patrones FT. Los fueras de juego permanecen en los datos estadísticos y quedan fuera de candidatos comerciales y explicaciones comerciales.

Una familia sin modelo conserva `MODELO_NO_DISPONIBLE`; los datos insuficientes conservan `INSUFICIENTE`. `PATRON_DETECTADO` es descriptivo. `MERCADO_INVESTIGABLE` exige el cruce existente y no demuestra EV. Esta implementación no produce `VALOR_VALIDADO`.

## Runtime LLM/RLM

No se encontró un runtime local genérico `ollama`, `llama-cli` o `rlm` durante la preparación. No se ha simulado su ejecución. El runtime es opcional como dependencia instalada, pero su investigación es obligatoria antes de cerrar el ranking enriquecido: mientras falte o no se autorice, se escriben los cuatro artefactos como pendientes y el ranking no se declara final.

Puede configurarse en `prepare` mediante `--llm-runtime-command-json '["/ruta/al/adaptador"]'`. El adaptador recibe JSON por stdin y devuelve JSON por stdout con `hypotheses` y `continue_research`. Las hipótesis deben incluir `hypothesis_id`, segmento completo, condición (o `all` de hasta tres condiciones), resultado, umbral, mecanismo, refutación y fixtures de entrenamiento citados. El esquema ejecutable y sus pruebas están en `prematch_llm_research.py` y `test_prematch_llm_research.py`.

El límite configurable es de 1–10 **invocaciones al adaptador**; el adaptador debe imponer sus propios límites de tokens, modelo y facturación interna. No hay shell para interpretar su comando. Se conservan las peticiones, respuestas y recibos. Las propuestas duplicadas o rechazadas no desaparecen del registro.

La partición fuera de muestra es global y cronológica por fixture: un encuentro reservado para un jugador no aparece como entrenamiento de su equipo. El LLM recibe observaciones y patrones iniciales Python calculados sólo con entrenamiento. El conjunto final reservado no se envía ni se usa como feedback entre iteraciones. Los crudos por lote originales pueden contener fixtures reservados: el runtime debe respetar la vista de entrenamiento y no abrirlos indiscriminadamente. El validador verifica hashes y evidencia, pero no puede acreditar por sí solo qué archivos abrió un runtime externo; tampoco autoriza uso predictivo o valor financiero.

La validación final aplica Fisher y ajuste Benjamini–Yekutieli al conjunto de propuestas contrastables, exige grupos de al menos tres observaciones y consistencia entre entrenamiento y prueba. Las asociaciones basadas en estadísticas del mismo partido permanecen posteriores al hecho; no son disparadores conocidos antes del kickoff.

## Qué se reutiliza del 12/11/2026

Raíz preservada: `outputs/phase1/2026-11-12_1500_0300/`.

| Artefacto | Hecho comprobado | Reutilización |
|---|---|---|
| `fixtures_discovered.json` | 73 fixtures, sin eliminar los de cobertura/categoría pendientes | Inventario congelado de esa ventana, no de otras fechas |
| `statistics_all_core_02/` | 73 fixtures; 2.154 HTTP nuevos y 630 hits, sin errores | Crudos y trazabilidad |
| `statistics_all_history20_01/` | Solicitud de hasta 20 antecedentes por equipo para los 73; 291 HTTP nuevos y 73 hits | Históricos y bloques incluidos en `/fixtures?ids=…` |
| `reconciled_02/` | Cuatro asociaciones API–Bwin verificadas en el snapshot | Identidad histórica; horario actual pendiente de refresco |
| `market_capture_01/`, `market_capture_retry_01/` | 613 selecciones entre cuatro eventos; un primer intento fallido conservado | Mercados/contratos capturados; nunca precios actuales garantizados |
| `statistics_period_probe_02/`, `statistics_periods_01/` | Dos pruebas iniciales y una pasada interrumpida: 941 consultas registradas, 124 de ligas y 817 de estadísticas con mitades; 938 respuestas API y tres de caché | Mitades y cobertura efectivamente obtenidas; quedan 42 consultas planificadas sin cerrar |
| `data_pattern_v1/provider_status_02.json` | Proveedor: 3.028/7.500 a las 03:03:12 UTC | Medición histórica; no saldo actual |
| `statistics_periods_01/interruption_verified.json` | Interrupción por cambio de alcance, con SHA del JSONL conservado | Reanudación auditada, sin completar consumo por estimación |

Los dos procesos interrumpidos no guardaron un resumen final completo. Sumando contadores completos, el checkpoint de 622 HTTP del primer proceso y los recibos conservados, el mínimo documentado es 4.051 HTTP; el techo conservador reservado fue 7.489. **El techo no es consumo real demostrado.** El contador de cuota del proveedor y los intentos HTTP no son unidades intercambiables. No se realizó otra consulta para actualizar ese saldo tras la prohibición del usuario.

Antes de una ejecución real nueva hay que actualizar inventario y estado de fixtures, asociaciones/horarios, cuotas Bwin, bajas, entrenadores y XI del objetivo. Los históricos compatibles se reutilizan. Las capturas antiguas no se arrastran como resultados o selecciones vigentes.

## Actualizaciones antes del kickoff

El recálculo de contexto existente sigue en `prematch_single.py refresh-context --snapshot … --snapshot-sha256 …`; consume un registro local fechado, verifica fixture y equipos y requiere los dos XI completos. Invalida derivados afectados; no añade ajustes probabilísticos inventados. Un XI manual necesita evidencia y una interfaz de ingestión con origen explícito; no debe disfrazarse de respuesta API ni de anuncio oficial.

`refresh-odds` reutiliza el mecanismo de captura y validación existente. Las acciones que consumen servicios requieren ahora `--confirm-external` explícito o la autorización heredada del controlador. Para los subcomandos single/multi, el flag global se coloca antes del nombre de la acción. La confirmación heredada se limita al proceso autorizado y se restaura al salir.

T−90, T−60, T−30 y T−15 son puntos de actualización bajo decisión del usuario, siempre antes del kickoff. No se ha instalado un programador ni polling automático. Los XI pueden no estar disponibles todavía en los primeros cortes. Un análisis congelado no sustituye la comprobación final de precio, identidad, horario y alineaciones pertinentes.

## Verificación

```sh
.venv/bin/python -B tools/run_offline_tests.py
```

El ejecutor bloquea red, procesos de adquisición y escrituras en los outputs originales durante las pruebas. Se comprueban el flujo sintético completo y su reanudación, las denegaciones sin aprobación, el presupuesto reservado antes del consumo, conservación de versiones de caché, diario persistente, mitades reales, exclusión de AET/PEN del conjunto FT, duelos sin interacción directa inventada, nulos frente a ceros y partición temporal sellada. El resultado final de la suite se registra en el informe de entrega de esta rama.

Resultado verificado en esta entrega: **658 pruebas, cero fallos, cero errores y diez omisiones documentadas** por fixtures históricos opcionales ausentes o configuración del entorno. El registro definitivo, con hashes del código probado, es `docs/verification_manual_execution_final_v2.json`; los dos registros anteriores se conservan como antecedentes de verificación. La interfaz manual y el flujo offline están verificados; una jornada real y la investigación con un runtime externo no se han ejecutado como parte de esta preparación.

Se dejó preparado y se comprobó con `dry-run` el plan `outputs/control/plans/20261112_reuse_offline_v1.json`: reutiliza el inventario y catálogo preservados, con presupuesto API cero, adquisición Bwin desactivada y runtime LLM sin configurar. La carpeta de ejecución de ese plan **no se ha creado**. Puede inspeccionarse sin consumo:

```sh
.venv/bin/python -B prematch_daily.py dry-run \
  --plan outputs/control/plans/20261112_reuse_offline_v1.json
```
