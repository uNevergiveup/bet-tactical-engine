# Fase 4A.1 — adquisición dirigida por segmentos individuales

Se extendió `prematch_history_recovery_v2.py`; no hay un recuperador paralelo.
Modo anterior preservado sin `--player-discovery`. ONLINE no ejecutado.

## Evidencia inspeccionada

Origen: `outputs/history_recovery_v2/release/20261109/discovery_new_version/`.
Confirmado: 3020 observaciones, 1433 individuales, 407 player_id únicos, 121 con
>=5 registros globales. Hay 429 segmentos individuales con entrenamiento
insuficiente, incluidos 55 con >=6 observaciones totales; 1166 asociaciones, sin
significación ajustada. No se sumaron ligas/temporadas para fabricar muestra.

El split existente conserva 70% antiguo para entrenar y el resto para holdout.
Se necesitan seis observaciones de entrenamiento, además de soporte para cada
condición y métrica. Seis registros totales suelen aportar cuatro de entrenamiento.
La participación, minutos y métricas ausentes reducen todavía más la muestra válida.
Tener nueve observaciones totales es solo una aproximación de alcanzar seis de
entrenamiento; empates de fecha y valores faltantes pueden impedirlo.

## Planificación implementada

Por segmento se guardan equipo/jugador/competición/temporada/género/tipo/período,
n total/entrenamiento/holdout, frontera cronológica original, cobertura de cada
métrica, déficit estimado y fixtures faltantes anteriores a esa frontera.
La frontera se usa para seleccionar adquisiciones, sin modificar el descubridor
ni su split. Tras añadir datos el algoritmo existente recalcula su partición;
no se garantiza una validación sobre el mismo holdout anterior.

Solo entran históricos conocidos en la preselección, con identidad consistente,
antes del cutoff y anteriores al holdout original del jugador. No se supone que
el jugador participara en esos partidos. La clasificación lexicográfica prioriza:
segmentos próximos a completar (déficit estimado <=2), jugadores beneficiables,
métricas previamente observadas y perspectivas compartidas. Fixture ID desempata.
Es utilidad estimada de adquisición, no probabilidad de resultado futbolístico.

Se reutiliza también la caché real del piloto referenciada por el manifiesto del
descubridor. SHA del manifiesto/fuente y artefacto histórico se comprueban antes de
planificar. Un endpoint completo ya existente no vuelve a solicitarse. Los planes
versionados permiten cambiar cobertura al reanudar sin sobrescribir el anterior.
Los bindings Bwin individuales no están acreditados en esta fuente y no reciben
bonificación; no se presume que existan mercados concretos.

En modo dirigido, para cada fixture se programan players, lineups y estadísticas
solo cuando faltan. No se agrupa players: ese endpoint no tiene agrupación
demostrada. Los fixtures elegidos ya tienen identidad reutilizable, por lo que no
hacen falta lotes de identidades. La vía general conserva sus lotes válidos de 20.
FOOTBALL_API_KEY se carga en ONLINE desde config/.env con load_dotenv, mecanismo
utilizado por config/constants.py, sin importar sus IDs globales ni imprimir claves.

## Resultado OFFLINE

126 fixtures priorizados, potencialmente vinculados a 384 de los 429 segmentos.
Los restantes no tienen un histórico conocido, pendiente y anterior a la frontera
que cumpla los filtros. 372 peticiones individuales como techo sin reintentos
(1116 con hasta tres intentos); no es presupuesto recomendado.

Ranking TOP25, formato fixture / jugadores potenciales / segmentos próximos:

| Rank | Fixture | Jugadores | Próximos |
|---:|---:|---:|---:|
|1|1519381|25|8|
|2|1519387|25|8|
|3|1519397|25|8|
|4|1519411|25|8|
|5|1519421|25|8|
|6|1519382|24|8|
|7|1519391|24|8|
|8|1519398|24|8|
|9|1519404|24|8|
|10|1519414|24|8|
|11|1519416|24|8|
|12|1519429|24|8|
|13|1519454|23|8|
|14|1519458|23|8|
|15|1519376|23|8|
|16|1519386|23|8|
|17|1519394|23|8|
|18|1519443|22|8|
|19|1519453|22|8|
|20|1519463|22|8|
|21|1519468|22|8|
|22|1519409|22|8|
|23|1519419|22|8|
|24|1519426|22|8|
|25|1519434|22|8|

Las cifras no se suman entre filas: los mismos jugadores comparten históricos.
El JSON enumera los IDs y cobertura de cada segmento potencialmente beneficiado.

| Presupuesto sin reintentos | Fixtures tocados dirigido | Segmentos potenciales distintos | Fixtures tocados anterior | Segmentos potenciales distintos anterior |
|---:|---:|---:|---:|---:|
|10|4|25|200|134|
|20|7|49|400|193|
|30|10|49|600|331|

El orden anterior abarca más calendarios mediante lotes. Esto no demuestra que
recupere más series individuales: se desconoce qué detalle embebido devolverá.
El dirigido concentra llamadas explícitas de players/XI en series conocidas. No
se afirma superioridad empírica ni recuperación real por esta simulación.

Salida definitiva:
`outputs/history_recovery_v2/player_directed_20261109/player_utility_plan_f7bd86f1911c74c1.json`.
Incluye ranking completo, 429 segmentos y escenarios 10/20/30. Plan y resultados
de recuperación se conservan junto a él. Cero llamadas nuevas, 1433 observaciones
individuales reutilizadas: no se contabilizan como observaciones recién adquiridas.

## Comandos exactos, desde la raíz del proyecto

```bash
task_source=outputs/discovery/2026-11-09_1500_0300/prospective_trial/enriched/preselection.json
task_discovery=outputs/history_recovery_v2/release/20261109/discovery_new_version
task_output=outputs/history_recovery_v2/player_directed_20261109

# Revisar plan OFFLINE
.venv/bin/python prematch_history_recovery_v2.py \
 --preselection "$task_source" --player-discovery "$task_discovery" \
 --output-dir "$task_output"

# Piloto ONLINE manual: máximo diez llamadas NUEVAS, incluidos reintentos
.venv/bin/python prematch_history_recovery_v2.py \
 --preselection "$task_source" --player-discovery "$task_discovery" \
 --output-dir "$task_output" --online --resume --max-api-calls 10

# Después de comprobar las respuestas y métricas realmente incorporadas
.venv/bin/python prematch_advanced_discovery_v2.py \
 --preselection "$task_source" --cache-root "$task_output/cache" \
 --allow-retrospective-cache --output-dir "$task_output/discovery_after_directed_pilot"
```

No se ejecutó el segundo comando. La recuperación es retrospectiva; adquisición
actual no acredita disponibilidad anterior al corte. Nuevas alineaciones históricas
no equivalen a XI oficial del próximo encuentro. No se generan picks ni modelos nuevos.

## Validación

277 tests PASS de las regresiones completas relevantes y test de segmentación,
frontera temporal y prioridad nueva. Integración OFFLINE real con sockets bloqueados,
dos ejecuciones idénticas (mismos archivos y SHA). Se mantienen tests de presupuesto,
reanudación, vacíos, TTL corto, HTTP y archivo raw inmutable de Fase 4A.
No se cambiaron umbrales ni el holdout para producir patrones. No se modificaron
snapshots originales, Predictor V1, descubridor ni Market Mapping.
