# Fase 3 MVP — ranking y paquete de investigación

Estado: FASE_3_READY_FOR_REAL_PILOT. Preparación deportiva; no autoriza apuestas.

## Auditoría y alcance

El comando diario ya disponía de descubrimiento, preselección, captura y handoff;
prematch_single mantiene los controles de check/prepare/refresh/finish. El cliente
de Fase 1 ya proporciona caché, SHA y presupuesto, y su recolector longitudinal
adquiere sólo metadatos, players y lineups del seleccionado. Fase 2 ya produce
patrones individuales/colectivos con denominadores y auditoría de integridad.
El punto desconectado era una ruta de investigación desde esos artefactos sin
exigir primero una captura Bwin. Se añaden opciones al mismo prematch_daily.py.

El modo nuevo recibe una preselección existente, comprueba su fuente canónica y
rankea localmente, sin profundizar todos los partidos. Si la preselección diaria
todavía no existe, debe producirse mediante el flujo de descubrimiento vigente;
este MVP no inventa datos ni convierte automáticamente un catálogo incompleto
de Fase 1 en estadísticas colectivas.

Ventana oficial predeterminada: 15:00–03:00 Europe/Madrid tanto para normal
como para ampliada. La fecha automática respeta el día anterior tras medianoche.
El ranking sin --preselection resuelve exclusivamente la carpeta de la jornada
solicitada (YYYY-MM-DD_1500_0300). No busca jornadas anteriores como alternativa.
Una ruta explícita con jornada/franja diferente se rechaza con diagnóstico;
las preselecciones con más de 120 minutos generan una advertencia persistida.
Una selección manual puede estar fuera de la ventana, pero no puede saltar
identidad, cutoff ni margen mínimo de preparación de 45 minutos.

## Ranking

Reutiliza analyze_fixture del motor táctico: producción/concesión, localía,
estabilidad, muestras, sensibilidad y contradicciones. Publica componentes,
evidencias por señal y fuente observacional/agregada. A la puntuación descriptiva
se restan contradicciones (5 cada una, máximo 20), ausencia de señal apoyada (10)
y descanso observado menor de 72h (3). El descanso es un indicador operativo,
no una inferencia de fatiga demostrada. Las métricas de jugadores, cuando tienen
identidad, corte y hashes comprobados, sólo aportan un máximo de 3 puntos.
No se interpreta la puntuación como probabilidad ni valor comercial.

Se conservan todos los candidatos y bloqueos en ranking.json y se muestran los
primeros cinco. Un cero de cobertura de detalle no equivale a irregularidad:
consultar la evidencia por familia; puede existir cobertura de goles agregados
sin datos de remates/córners. No se exige cobertura individual para continuar.

## Comandos

Desde /Users/antonio/Projects/scraper, ranking sin red:

```bash
.venv/bin/python prematch_daily.py --rank \
 --preselection outputs/discovery/2026-11-07_1500_0300/prospective_trial/enriched/preselection.json
```

Preparación automática del primero del ranking, exclusivamente con caché:

```bash
.venv/bin/python prematch_daily.py --prepare-fixture \
 --preselection outputs/discovery/2026-11-07_1500_0300/prospective_trial/enriched/preselection.json
```

Prueba PREMATCH real seleccionando el fixture observado 1583806, con adquisición
selectiva autorizada de hasta 10 llamadas nuevas (no ejecutada en esta tarea):

```bash
.venv/bin/python prematch_daily.py --prepare-fixture --fixture-id 1583806 \
 --preselection outputs/discovery/2026-11-07_1500_0300/prospective_trial/enriched/preselection.json \
 --online --max-api-calls 10
```

Este comando bloqueará el fixture cuando empiece o no conserve 45 minutos.
Para otro día debe usarse su preselección vigente. --fixtures-input permite
indicar explícitamente el catálogo canónico; por defecto se localiza junto a
la preselección. --date fuerza el día operativo y --asof permite un replay
retrospectivo que bloquea adquisiciones online y habilitación comercial.

El presupuesto máximo inicial del recolector selectivo existente es 25. La red
sólo se utiliza con --online al preparar un único fixture, nunca en Fase 2.

## Identidad y seguridad

Se verifican IDs, localía, competición, temporada, categoría/género, NS, kickoff,
cutoff y SHA canónico. La identidad API de caché no acredita estado actual del
proveedor. Para habilitar la vinculación comercial se necesita --preflight con
originales accesibles y catálogo API fechado de las últimas dos horas. Se llama
al verified_preflight existente: SHA de discovery, TXT original, Event ID,
timestamp, antigüedad, localía, categoría y tablas por evento. Nunca se utilizan
booleans previos como sustituto de la revalidación. Aun cuando ésta pase,
actionable_bet sigue false y los controles posteriores no se omiten.

Cada ejecución tiene un directorio nuevo runs/<timestamp>/fixtures/<fixture_id>.
El paquete guarda hashes de origen, corte, edad del contexto, ranking,
patrones individuales/colectivos, señales tácticas, faltantes y razones Bwin.
El brief exige fuentes fechadas, hechos/inferencias y evidencia contraria.
XI, bajas, noticias, tácticas y cuotas recientes siguen pendientes cuando falten.
El contexto antiguo se identifica; no se presenta como actualizado por repetir
el procesamiento offline.

## Ejecución real local y pruebas

Paquete generado con reloj actual y cero consultas nuevas:
outputs/phase3/real_cached_pilot/runs/20261107T155809334028/fixtures/1583806/.
San Francisco FC–Union Cocle, kickoff del catálogo: 7/11/2026 22:00 Madrid.
Una señal táctica descriptiva; Fase 2 produce 2 patrones apoyados, 14 con muestra
insuficiente y 4 débiles. No hay observaciones individuales disponibles para
este encuentro. La cobertura de detalle es insuficiente; se conservan las
estadísticas de goles disponibles, sin inventar remates ni jugadores.

Bwin: BWIN_ORIGINAL_PREFLIGHT_NOT_PROVIDED; Event ID null. Falta evidencia Bwin
original verificable y cuotas frescas. market_identity_ready=false y
actionable_bet=false. No se ejecutó scraper, IA externa ni motor de apuestas.

El replay adicional de 1550188 queda bajo outputs/phase3/pilot_1550188 y está
marcado RETROSPECTIVE_REPLAY, usando la caché longitudinal ya adquirida.

147 tests relacionados aprobados en la ejecución conjunta; tras los ajustes de
presentación y freshness, los cinco tests nuevos se repitieron y pasaron.
Cubren ranking reproducible, selección manual/automática, aislamiento, SHA,
cobertura parcial, falta de Bwin, conflictos de identidad, LIVE, inicio y presupuesto.
Las regresiones anteriores cubren caché, temporalidad, mercado y Fases 1/2.

```bash
.venv/bin/python -m unittest -q test_prematch_phase3.py test_prematch_daily.py \
 test_prematch_phase1.py test_prematch_phase2.py \
 test_prematch_phase2_player_integrity.py test_prematch_longitudinal.py \
 test_prematch_tactical_signal_engine.py test_prematch_pattern_discovery_v2.py
```

Archivos de esta tarea: prematch_daily.py, config/prematch_windows.json,
test_prematch_phase3.py y este documento. Los artefactos históricos se conservan.
