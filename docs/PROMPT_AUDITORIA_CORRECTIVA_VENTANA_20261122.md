# Prompt de auditoría correctiva PREMATCH — ventana 22/11/2026

Actúa como auditor independiente del flujo PREMATCH multimercado. Revisa de
forma retrospectiva la corrida completa de la ventana del 22 de noviembre de
2026, sin navegar por Internet y sin volver a ejecutar scraper, API-Football,
Shadow, generadores ni modelos.

## Alcance

Usa exclusivamente los artefactos locales de esta corrida:

```text
/Users/antonio/Projects/bet-tactical-engine/outputs/discovery/2026-11-22_1400_0300_fase1_20261122T005303966107Z
```

Incluye todos los lotes horarios y todos los refrescos de esa corrida,
especialmente:

```text
hour_batches/
prospective_trial/
fixtures.json
fixtures_major.json
ranking_fase1.md
```

No mezcles archivos de otras corridas, otros días, otros fixtures ni datos
posteriores al kickoff. Si encuentras rutas compartidas, comprueba que el
Fixture ID y el Event ID pertenecen al partido que estás auditando.

## Objetivo principal

Determina por qué tantos resultados terminaron en `NO BET`, `LISTO_REVISION_FINAL`,
`PRECIO_PENDIENTE`, `XI_PENDIENTE` o `CIERRE_OPERATIVO`, y separa la causa real
de la etiqueta final.

No presupongas que `NO BET` es correcto ni incorrecto. No presupongas que una
ausencia de dato invalida todo el análisis. No presupongas que Shadow debe
decidir el resultado final.

## Regla fundamental sobre Shadow

Lee el modelo Shadow como una fuente cuantitativa más, no como una orden ni como
un veto automático. El LLM final debe analizar primero el partido, el mercado,
la cuota, la liquidación, la evidencia futbolística y las incertidumbres. Solo
después debe comparar su conclusión con Shadow.

No escribas instrucciones del tipo:

- “si Shadow dice NO BET, debes concluir NO BET”;
- “solo puedes elegir si Shadow marca PICK_ACCIONABLE”;
- “la falta de calibración bloquea automáticamente el partido”.

Si Shadow rechaza una opción, explica si el rechazo se debe a calibración,
muestra, precio, contrato, incertidumbre o a una regla operativa. Determina si
ese rechazo es una limitación informativa o un bloqueo que el flujo impuso sin
justificación suficiente.

## Revisión de cada partido

Para cada Fixture ID y Event ID de la ventana, reconstruye la secuencia:

1. descubrimiento y filtro;
2. vínculo Bwin e identidad;
3. histórico y ranking;
4. captura inicial de cuotas;
5. catálogo completo y mercados no implementados;
6. `encargo_codex_argon.md` y `prefinalistas.json`;
7. investigación cualitativa disponible;
8. refresco de cuotas;
9. XI disponible, ausente, manual o incompleto;
10. Shadow;
11. prompt final;
12. resultado final del LLM, si existe;
13. estado de cierre y motivo impreso en `cierre.json`.

Para cada etapa registra la ruta exacta del archivo usado y el timestamp. No
confundas `mtime` con hora de captura declarada por el artefacto.

## Clasificación obligatoria de resultados

Cada partido debe recibir una clasificación principal y, si corresponde, una
secundaria:

### A. Bloqueo operativo

Ejemplos:

- kickoff demasiado cercano;
- captura iniciada fuera del margen;
- precio caducado;
- XI no acreditado cuando era obligatorio;
- snapshot rechazado por identidad, hash o timestamp;
- scraper fallido;
- archivo faltante.

Esto no es una conclusión futbolística y no debe presentarse como “el modelo
decidió NO BET”.

### B. Decisión cuantitativa del modelo

Existe un `model_report` que compara mercados y concluye `NO BET` por razones
como muestra insuficiente, banda inferior negativa, precio insuficiente o falta
de calibración.

Explica la razón concreta y qué mercados quedaron realmente comparados.

### C. Decisión cualitativa del LLM final

El LLM leyó el expediente y decidió `PICK`, `NO BET` o una conclusión
exploratoria. Comprueba si razonó de forma independiente o si repitió
mecánicamente el veto de Shadow.

### D. Bloqueo artificial del prompt o del código

Identifica frases, filtros o condiciones que hayan convertido una limitación en
un veto automático. Ejemplos:

- falta de XI tratada como veto para mercados colectivos resistentes;
- falta de Gemini tratada como bloqueo;
- mercado no implementado tratado como cuota inexistente;
- ausencia del árbitro tratada como invalidación universal;
- cualquier `NO BET` heredado sin análisis del contrato;
- obligación de que Shadow sea `PICK_ACCIONABLE` antes de que el LLM pueda
  razonar;
- cierre por tiempo presentado como decisión del modelo.

## Tratamiento de datos ausentes

La ausencia de información no debe describirse de forma repetitiva ni como una
condena general. Usa estas categorías:

- `DATO DISPONIBLE Y USADO`;
- `DATO AUSENTE, IMPACTO LOCALIZADO`;
- `DATO AUSENTE, IMPACTO RELEVANTE EN ESTE MERCADO`;
- `DATO AUSENTE, NO CRÍTICO PARA ESTE MERCADO`;
- `DATO CONFLICTIVO`;
- `DATO NO ACREDITADO`.

Para cada dato faltante indica exactamente qué mercado afecta. Por ejemplo,
una alineación no confirmada puede afectar un prop de jugador y no bloquear
automáticamente un total colectivo. La ausencia de Gemini reduce cobertura
cualitativa, pero no es por sí sola una razón de `NO BET`.

No inventes alineaciones, árbitros, lesiones, cuotas, probabilidades ni
fuentes. Si falta un dato, dilo una sola vez con su impacto concreto.

## Auditoría específica de liquidación (`SETTLEMENT_UNVERIFIED`)

No trates todas las etiquetas `SETTLEMENT_UNVERIFIED` como equivalentes.
Clasifica cada una de estas situaciones por separado:

1. **Regla de liquidación realmente desconocida**: no está acreditado qué
   proveedor usa Bwin, cómo cuenta prórroga, doble amarilla, correcciones o la
   definición exacta del mercado.
2. **Mercado capturado pero parser no implementado**: la cuota existe en el TXT
   y en el catálogo, pero el motor no tiene todavía un contrato canónico.
3. **Contrato conocido pero conciliación pendiente**: existe una regla o familia
   afín que podría validarse con evidencia local, pero falta registrar la
   conciliación.
4. **Mercado legítimamente no comparable**: la selección no puede compararse con
   el histórico disponible o mezcla una unidad distinta (por ejemplo, córners
   del equipo frente a córners totales).

Para cada mercado afín —por ejemplo `0-3`, `4-6`, `7+`, primera parte,
segunda parte o `Ambos equipos 4+ córners`— indica si el problema es de
liquidación, de parser, de evidencia estadística o solo de falta de integración
en el lote. No digas que la cuota “no existe” si aparece en el TXT.

Comprueba si la etiqueta está provocando un veto demasiado amplio. Un mercado
con liquidación no verificada puede conservarse como:

- `ELEGIBLE_VERIFICADO`;
- `ELEGIBLE_CONCILIACION_PENDIENTE`;
- `CANDIDATO_EXPLORATORIO_NO_EXECUTABLE`;
- `DESCARTADO_POR_REGLA_REALMENTE_DESCONOCIDA`.

La auditoría debe proponer qué campos, contrato, prueba offline o fuente local
permitirían pasar de una categoría a otra. No conviertas automáticamente
`SETTLEMENT_UNVERIFIED` en `NO BET` financiero si la razón real es que el parser
no lo implementa.

## Antigüedad y estabilidad de las cuotas

Analiza si el flujo está siendo demasiado estricto con la edad de los
snapshots. Compara, por el mismo contrato y selección:

- hora de captura inicial;
- hora de captura de cierre;
- diferencia de precio;
- cambio de línea;
- publicación de XI entre ambos snapshots, si está acreditada;
- minutos hasta kickoff;
- disponibilidad del mercado en cada captura.

No asumas que toda cuota cambia mucho al acercarse el inicio ni que nunca
cambia. Determina empíricamente, con los snapshots locales, si el precio se
mantuvo estable o si hubo movimiento material.

Propón una política escalonada, no un veto binario por antigüedad:

1. **Snapshot reutilizable**: misma selección, misma línea, identidad y hash
   válidos, sin movimiento material y dentro de la ventana operativa.
2. **Snapshot estable pero antiguo**: puede alimentar análisis y shortlist,
   pero requiere una alerta visible de `PRECIO_ANTIGUO` y una comprobación final
   si todavía hay tiempo.
3. **Movimiento material**: exige revaluar la selección y el break-even.
4. **Línea desaparecida o contrato cambiado**: no reutilizar como si fuera el
   mismo mercado; revisar líneas vecinas por separado.

Define “movimiento material” con un criterio documentado y configurable, no con
una intuición del LLM. El análisis debe señalar si el sistema está descartando
un candidato defendible simplemente porque un filtro de frescura se aplicó de
forma acumulativa después de que ya hubiera pasado otros controles.

No elimines la alerta de precio: reemplaza el bloqueo automático cuando la
cuota sea antigua pero estable, y conserva la exigencia de verificación final
cuando el mercado pueda haber cambiado.

## Auditoría de profundidad histórica y mercados por período

Comprueba si el histórico utilizado es suficiente para detectar tendencias como:

- gol en la primera parte en los últimos 10, 15 o 20 partidos;
- gol en la segunda parte en los últimos 10, 15 o 20 partidos;
- ambos equipos marcan en una parte concreta;
- córners, tarjetas, tiros o faltas por primera y segunda parte;
- secuencias recientes que no aparecen con una muestra demasiado corta.

No sustituyas automáticamente el histórico actual por una muestra grande. Compara
varias ventanas y explica qué cambia:

- últimos 5;
- últimos 10;
- últimos 15;
- últimos 20;
- temporada completa disponible;
- solo liga local;
- todas las competiciones.

Para cada ventana informa `n`, partidos ausentes, competición, localía,
entrenador vigente y si existe fuga temporal. Prioriza la liga local cuando la
hipótesis sea sobre el comportamiento regular del equipo, pero conserva una
columna de copas, playoff e internacional para detectar cambios de régimen.

Cruza el histórico con:

- calendario y congestión;
- competición y fase;
- localía;
- tabla y necesidad de puntos, clasificación o permanencia;
- partidos siguientes y rotación probable;
- bajas, sanciones y cambios de entrenador;
- fuerza y estilo del rival;
- XI confirmado o condición de XI pendiente;
- goles a favor y en contra por primera y segunda parte.

Una tasa como “85% en 20 partidos” es descriptiva hasta que se compruebe el
denominador, la homogeneidad de la muestra, los partidos sin datos y el régimen
táctico. No la conviertas automáticamente en probabilidad propia ni en valor.
Indica si el patrón es estable, reciente, contextual, contradictorio o una
posible ilusión de muestra.

## Caso diagnóstico obligatorio: fixture 1651120

Usa Vancouver Whitecaps–Real Salt Lake (`1651120`, Event ID `7917640`) como
caso para comprobar estas preguntas:

1. ¿El `NO BET` provino de la tesis futbolística, de Shadow, de la falta de
   calibración o de una cadena de filtros?
2. ¿El mercado `Real Salt Lake - Total de córners >3,5 @1.75` estaba realmente
   liquidado como `VERIFIED_STANDARD`?
3. ¿Las cuotas afines de córners que aparecen en Bwin quedaron fuera por
   liquidación desconocida, por parser o por falta de evidencia?
4. ¿La captura de `22:52 UTC` seguía siendo estable respecto a la inicial de
   `22:29 UTC`?
5. ¿La ausencia de XI era crítica para el total colectivo o solo para el prop
   de Saba Lobzhanidze?
6. ¿Un histórico de córners más amplio, separado por MLS local, playoff y
   localía, cambia la clasificación descriptiva?
7. ¿El resultado previo presentó como decisión del modelo lo que en realidad
   era una limitación operativa o de calibración?

No fuerces un PICK para 1651120. El objetivo es localizar qué parte del
razonamiento fue válida, cuál fue demasiado restrictiva y cómo debe cambiar el
flujo para futuros encuentros.

## Auditoría del prompt final

Compara los prompts finales generados por el código con los resultados que
produjo cada LLM. Busca:

1. instrucciones que condicionen la decisión antes del análisis;
2. restricciones que impidan considerar una tesis razonable;
3. mezcla de partidos dentro de un prompt;
4. referencias a archivos inexistentes o de otro evento;
5. exigencias contradictorias sobre XI, precio o liquidación;
6. diferencia entre el estado operativo y la decisión financiera;
7. lenguaje excesivamente rígido que repita “no hay datos” sin analizar el
   efecto real;
8. ausencia de revisión de mercados vecinos y líneas alternativas;
9. imposibilidad de producir una hipótesis condicional cuando el valor no está
   acreditado, pero la tesis futbolística sí es defendible.

## Auditoría del código

Inspecciona selectivamente el código que produce:

- `hour-batch-prepare`;
- `hour-batch-refresh`;
- selección de candidatos por XI;
- evaluación de precio y liquidación;
- integración de Shadow;
- generación de `revision_final_codex_argon.md`;
- clasificación de estados y motivos.

Distingue defectos de código de errores de uso, argumentos incorrectos o pasos
ejecutados fuera de margen. No atribuyas al código un fallo causado por usar el
JSON equivocado, una hora errónea o un snapshot inexistente.

## Tabla obligatoria de resultados

Genera una tabla con una fila por partido y estas columnas:

```text
fixture_id
event_id
partido
kickoff_madrid
estado_operativo
decision_shadow
decision_llm_final
causa_principal
causa_secundaria
mercados_comparados
mercados_no_implementados
datos_faltantes
impacto_real_de_los_faltantes
prompt_condicionado
defecto_de_codigo
error_operativo
acción_correctiva
```

## Conclusiones requeridas

Entrega las conclusiones en este orden:

1. cuántos partidos fueron realmente cerrados a tiempo;
2. cuántos quedaron bloqueados solo por operación;
3. cuántos recibieron `NO BET` cuantitativo;
4. cuántos recibieron `NO BET` cualitativo;
5. cuántos fueron condicionados indebidamente por Shadow;
6. cuántos quedaron afectados por falta de datos y qué mercados concretos
   sufrieron ese efecto;
7. qué problemas son del código;
8. qué problemas fueron de comandos o procedimiento;
9. qué cambios deben hacerse al prompt final;
10. qué cambios deben hacerse al motor y qué pruebas offline los validan;
11. qué mercados quedaron injustamente fuera por `SETTLEMENT_UNVERIFIED`;
12. qué snapshots antiguos eran reutilizables con alerta en lugar de bloqueo;
13. qué tendencias de primera/segunda parte requieren ampliar histórico;
14. qué cortes por liga local y contexto competitivo deben añadirse.

## Propuesta de prompt final corregido

Al terminar la auditoría redacta una versión corregida del prompt final que:

- obligue a verificar identidad y tiempo;
- permita analizar con datos incompletos;
- permita usar un snapshot antiguo y estable con una alerta explícita;
- exija revaluación cuando haya movimiento material de cuota o línea;
- no convierta la falta de XI, Gemini o árbitro en veto universal;
- diferencie liquidación desconocida, parser incompleto y mercado exploratorio;
- solicite ventanas históricas 5/10/15/20 y separación liga local/otras
  competiciones cuando la hipótesis sea temporal;
- no obligue a seguir el resultado de Shadow;
- exija comparar Shadow después del razonamiento futbolístico;
- distinga hipótesis futbolística de ventaja financiera demostrada;
- separe `PICK`, `NO BET`, `CANDIDATO EXPLORATORIO` y bloqueo operativo;
- pueda producir conclusiones condicionales por mercado;
- limite el análisis a un fixture y Event ID;
- conserve las rutas y hashes de los archivos usados.

La auditoría debe ser un diagnóstico local. No reescribas expedientes históricos,
no cambies resultados ya guardados y no presentes una reconstrucción como si
fuera una decisión ejecutable original.
