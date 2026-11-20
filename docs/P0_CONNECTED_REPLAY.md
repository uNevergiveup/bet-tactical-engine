# Evidencia de la prueba PREMATCH encadenada

Estado global: **PARTIAL**. Replay encadenado: **PASS**.

Se añade `test/test_prematch_connected_e2e.py`. Ejecuta un único expediente
sintético 7000001/99 en un directorio temporal, con reloj controlado solo en
tests. No usa información deportiva real ni consulta proveedores. No cambia
archivos de Gemini/Grok, probabilidades, reglas comerciales ni márgenes.

## Recorrido observado

1. Replay de entrada de descubrimiento por `bwin_discovery.read_candidates`.
2. Ranking descriptivo real por `prematch_pattern_engine.analyze`.
3. Elección manual explícita representada mediante el contrato research_selection.
4. `promote_research` real: selector estricto, hashes y contratos temporales.
5. Caso canónico ERROR_CONTEXTO, nunca PREPARADO anticipadamente.
6. `retry_context_one` real: pinning de TXT/JSON, identidad y estado PREPARADO.
   Se sustituye exclusivamente su invocación del proceso generador por bytes
   sintéticos y su log; no se ejecuta generate_context.main de extremo a extremo.
7. Rechazo real de precio envejecido y refresco permitido mediante cmd_refresh.
8. Validador y motor reales sobre el evento sintético, generación de CODEX.
9. Actualización local de XI por cmd_refresh_context y nueva validación/motor.
10. Nuevo refresco de cuotas, revalidación y nueva preparación de entrada Codex.

Los pasos 8–10 reutilizan el ciclo offline existente en la misma instancia y
el mismo caso creado por promote_research, no casos independientes pegados.
El fixture sintético heredado se utiliza como respuesta del proveedor; su
manifiesto inicial se retira antes de empezar para exigir la adopción real.
Los ejecutables de adquisición nunca se ejecutan; el validador y motor sí.

## Reutilización y recálculo selectivo

El historial de entrada conserva su SHA-256 durante todo el recorrido. No se
invoca descubrimiento después de su replay inicial. La sustitución de procesos
rechaza cualquier etapa no esperada.

Se comprobó que `prematch_context_delta.EvidenceCache/evidence_key` y el motor
ya implementan reutilización según dependencias. Después del cambio de XI:
**una evidencia recalculada, una reutilizada**. Después del cambio solo de cuota:
**cero evidencias deportivas recalculadas**, al menos una reutilizada; precio,
validación y ranking se reconstruyen. No hacía falta otro ejecutor o motor
para completar esta transición: finish explícito utiliza esa caché.

No se implementó un delta general de lesiones, entrenador o todos los campos
del contexto. El contrato existente cubre XI con procedencia y conciliación;
es suficiente para el recorrido probado, pero no acredita cambios generales.

## Errores y corrección mínima

La prueba verifica rechazo de Event ID incorrecto, archivo ausente, precio
obsoleto, adopción fuera de T-45, actualización fuera de T-5 y postkickoff.
La cobertura focalizada adicional conserva casos de equipos invertidos, género,
liga/temporada, hashes, XI incompletos y snapshots posteriores al inicio.

Único cambio productivo nuevo: `prematch_case.promote_research` incluye los
motivos originales del selector en MANUAL_TRANSITION_BLOCKED. Antes devolvía
un diagnóstico genérico. No se cambia el resultado del filtro.

Durante la construcción se corrigió una ruta del fixture sintético: colocar
los controles en root/run hacía que el resolvedor considerase inputs fuera del
proyecto; se movieron a la estructura outputs/discovery/run admitida. Otro
rechazo por edad se resolvió refrescando, no ampliando el límite. No eran
defectos del control de producción.

Los dos errores legacy de ../prematch_multi.py siguen siendo baseline, ya
reproducidos con HEAD en la intervención anterior. Esta prueba reutiliza el
harness con rutas temporales correctas; no modifica aquellos tests.

## Ejecución y evidencias

```bash
cd /Users/antonio/Projects/bet-tactical-engine
PREMATCH_E2E_EVIDENCE_DIR=outputs/audits/p0_connected_offline PYTHONPATH=.:test .venv/bin/python -B -m unittest test_prematch_connected_e2e.ConnectedReplay.test_connected_replay
```

Resultado: **1 prueba encadenada PASS**. Evidencias:

- outputs/audits/p0_connected_offline/connected_replay.json: traza y cachés de
  las tres evaluaciones; cero llamadas externas.
- synthetic_validation.json: salida real del validador sintético.
- synthetic_codex_prompt.md: entrada real generada para Codex, no ejecutada.

Las referencias temporales dentro de esas copias son evidencia del test,
no un expediente operativo reutilizable: el directorio temporal se elimina.

Regresión combinada: **130 pruebas; 129 aprobadas y una omitida**, con la prueba
encadenada y los módulos focalizados del informe P0 anterior. Sin fallos nuevos.
`git diff --check` también pasa. No se acredita la suite global completa.

## Qué continúa sin acreditarse

El replay comienza en un snapshot de preselección y representa la elección
manual mediante su contrato. No ejecuta la adquisición inicial de fixtures ni
la construcción completa de estadísticas desde respuestas API crudas. La
adquisición completa de contexto se sustituye en el límite del subproceso,
no exclusivamente en el cliente HTTP. Por ello no demuestra todavía la cadena
completa de normalización/adquisición del generador bajo sus respuestas API.

La integración local desde esos contratos hasta la preparación para Codex sí
está probada en un solo caso. El delta general de contexto sigue pendiente,
y no se declara READY global ni se transforma un replay en verificación online.
