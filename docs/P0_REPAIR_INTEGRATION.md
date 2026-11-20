# Reparación P0 — identidad, adopción manual y actualizaciones

Estado: **PARTIAL**. Ingeniería offline; no se ejecutó una nueva jornada, API,
Bwin ni una decisión deportiva. Rama: codex/prematch-data-pattern-v1. Se
conservaron los cambios previos, sin commits ni integración de otras ramas.

## Causas y correcciones

1. `generate_context.py` cargaba evidencia temporal de selección, pero después
   volvía a buscar clubes por textos/slug. Ahora una selección verificada fija
   Fixture ID, IDs local/visitante, competición y temporada. Se consulta el
   fixture sin caché para verificar su estado actual. Cualquier discrepancia
   es terminal: no hay rescate nominal tras un conflicto de IDs. La categoría
   debe estar corroborada por el catálogo existente; desconocida sigue
   bloqueada para esta ruta. Etiquetas Bwin contaminadas no eligen clubes.
   Un contexto por selección fijada no vuelve a descargar HTML Bwin.
2. `prematch_case.py promote-research` ofrece una transición **manual explícita**
   desde investigación. Verifica hashes originales y vuelve a cruzar fuentes
   actuales mediante el selector estricto, T-45, cuotas <=120 minutos y
   evidencia Bwin <=180 minutos. La elección explícita de un Event ID no es
   una comparación automática: el mínimo automático de dos no cambia.
   El expediente se crea únicamente después de pasar controles. No se fabrica
   un handoff diario. El campo legado source_handoff referencia la fuente real
   de investigación y source_kind identifica MANUAL_RESEARCH_TRANSITION.
3. La adopción diaria y manual comparten la misma función. Las cuotas se fijan
   en inputs mediante creación exclusiva; se conserva la captura archivada.
   Manifest registra scraper=0, porque ya se validó esa captura, y conserva su
   origen. Sin contexto completo el estado es ERROR_CONTEXTO/next_action
   retry-context, **no PREPARADO**. La ventana de retry puede resolverse desde
   el case y selección fijados por hash, sin inventar una hora.
4. `select_single_match.py` transporta IDs API, liga, temporada y género tanto
   en ranking como en selección automática. No se repite el arreglo anterior
   de api_kickoff_madrid; continúa vigente y probado.
5. `prematch_single.py prepare` transmite la selección fijada al generador.
   La recuperación de contexto existente ya transmitía ese contrato. Antes de
   regenerar, `prematch_multi.py` archiva por hash los inputs anteriores; el
   manifiesto distingue previous_context_snapshot/current=false del intento
   nuevo, incluso si falla. No presenta el snapshot retenido como refrescado.
6. `refresh-odds` mantiene su archivo anterior y añade odds_delta.json: contratos
   exactos añadidos, no observados y cambios de cuota. No observado no implica
   retirado ni suspendido. Se invalidan evaluación de precios, validación y
   análisis final; los históricos no se adquieren otra vez.
7. `acquire-lineups` consulta únicamente el Fixture ID elegido, comprueba el
   fixture actual y guarda la respuesta nueva con timestamp y hash mediante
   acquisition_record. Si falta XI o está incompleto registra
   XI_ABSENT_OR_INCOMPLETE sin sustituir el contexto. Con ambos XI completos
   reutiliza refresh-context y su conciliación de IDs. Son alineaciones API
   estructuradas, no certificación de anuncio oficial.
8. `prematch_case.py status --event-id` localiza una investigación manual
   aunque todavía no exista case.json. Devuelve RESEARCH_ONLY con referencias;
   no depende de conocer la carpeta diaria y no crea una selección.

No se modifican prematch_daily.py, módulos tácticos ni empaquetador IA.

## Estados y límites

- RESEARCH_ONLY: expediente exploratorio sin adopción operativa.
- SELECTED_FOR_DEEP_RESEARCH: elección explícita validada, no apuesta.
- ERROR_CONTEXTO + next_action=retry-context: adopción pendiente de contexto.
- PREPARADO: contexto e identidad canónica disponibles.
- Refresco: PREPARADO con invalidated_by_odds_refresh/context_refresh; exige
  nueva validación. Un input nuevo no vuelve válidos los resultados anteriores.
- VALIDADO/FINALIZADO y bloqueos existentes conservan sus contratos.
- Ninguno de esos estados equivale por sí solo a autorización de apuestas.

T-45 continúa siendo requisito de adopción/preparación inicial; las operaciones
incrementales conservan T-5. No se cambia el reloj para la operativa real.
Granada es un caso de regresión histórico, no un objetivo que pueda reabrirse
después de sus ventanas temporales.

## Validación

Comando focalizado, sin proveedores externos:

```bash
cd /Users/antonio/Projects/bet-tactical-engine
PYTHONPATH=.:test .venv/bin/python -B -m unittest test_p0_repair test_generate_context_identity test_prematch_case test_prematch_context_delta test_select_single_match test_temporal_contract_transport test_prematch_prospective_trial
```

Resultado: **129 pruebas ejecutadas; 128 aprobadas, una omitida**. Incluye
Granada 715 frente al ID incorrecto 22018, localía invertida, liga/temporada,
género, kickoff, bloqueo temporal, adopción manual por el selector y consumidor
reales, ausencia de XI, conservación de históricos, diferencias de cuotas y
ciclo offline de validación/motor/análisis sintético. No se acredita adquisición
real por aprobar mocks de proveedor.

El ciclo final legacy tiene dos errores preexistentes: sus argumentos
../prematch_multi.py apuntan fuera del root temporal. Se reprodujeron cargando
prematch_single.py de HEAD en memoria, sin restaurar archivos. El nuevo harness
P0FinalCycleTests reutiliza ese ciclo con la ruta del placeholder correcto,
sin modificar su archivo ni debilitar el control de producción. Sus cinco
pruebas pasan, incluido refresh y revalidación. Otros ensayos legacy de contratos
temporales/reutilización dependen de fixtures ausentes en test/outputs; no se
alteraron sus rutas para aparentar una suite global verde. La suite global no
está acreditada. `git diff --check` pasa.

CLI comprobado mediante --help: promote-research y acquire-lineups. Las pruebas
ejercitan las interfaces Python reales; los siguientes comandos externos son
instrucciones para ejecución manual, **no una jornada ya verificada online**.

## Comandos de la próxima prueba manual

Para descubrimiento, estadísticas, ranking y captura, usar los comandos existentes
de PREMATCH_MANUAL_PHASES.md, con un RUN nuevo. No reutilizar el handoff de Granada.
La ventana 15/11/2026 15:00 → 16/11/2026 03:00 no autoriza encuentros ya iniciados.
Si ya transcurrió, se debe configurar otra ventana futura; no simular --asof.

Después de producir research_selection.json, para el Event ID elegido por el
usuario y fuentes Bwin actuales:

```bash
cd /Users/antonio/Projects/bet-tactical-engine
PY=.venv/bin/python
# RUN: directorio real de la nueva ejecución; EVENT_ID: elección explícita.
"$PY" -B prematch_case.py promote-research \
  --research-selection "$RUN/selection/research_selection.json" \
  --discovery "$RUN/bwin/bwin_discovery.json" \
  --preflight "$RUN/capture/market_preflight.json" --event-id "$EVENT_ID"
```

La salida es la ruta exacta del selected_match.json nuevo. Usarla como SELECTED:

```bash
"$PY" -B prematch_case.py status --event-id "$EVENT_ID"
"$PY" -B prematch_case.py acquire-context --selected "$SELECTED"
"$PY" -B prematch_single.py --selected "$SELECTED" --confirm-external refresh-odds
"$PY" -B prematch_single.py --selected "$SELECTED" --confirm-external acquire-lineups
```

Comprobar case.json, manifest.json, inputs/match_identity.json,
inputs/match_engine_<EVENT_ID>.json, inputs/cuotas_bwin_<EVENT_ID>.txt,
archives/*/odds_delta.json, context_acquisitions y context_updates. Si un paso
falla, conservar su diagnóstico y no ejecutar finish como si hubiese pasado.

Importación local de un registro API adquirido antes del kickoff:

```bash
"$PY" -B prematch_single.py --selected "$SELECTED" refresh-context \
  --snapshot "$XI_RECORD" --snapshot-sha256 "$XI_SHA256"
```

No convertir XI de prensa en un registro API. Con informes y datos resueltos,
la ruta existente de validación puede ejecutarse explícitamente:

```bash
"$PY" -B prematch_single.py --selected "$SELECTED" finish --report "$REPORT"
"$PY" -B prematch_case.py export-handoff --event-id "$EVENT_ID"
```

Estos comandos no se ejecutaron sobre partidos reales durante la reparación.

## Pendientes que impiden READY

- La planificación diferencial identifica e invalida módulos afectados, pero
  la reevaluación sigue siendo una operación explícita de finish. No se ha
  implementado un ejecutor que recalcule automáticamente solo esos módulos.
- La actualización incremental nueva adquiere XI, no un delta general de bajas,
  entrenadores y demás contexto; el contexto completo utiliza la recuperación
  existente, conservando cachés e inputs anteriores.
- El smoke test cubre piezas conectadas y un ciclo final sintético separado;
  falta acreditar una única prueba que encadene desde descubrimiento hasta
  adquisición completa canónica y revisión final con todos los contratos.
- No se han integrado estadísticas ni paquetes IA de Gemini/Grok, por instrucción.

Por esos motivos la nueva transición es utilizable y tiene regresiones, pero
no se declara el objetivo PREMATCH end-to-end completo ni READY.
