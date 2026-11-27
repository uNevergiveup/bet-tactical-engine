# PREMATCH por lotes horarios con `prematch_steps.py`

## 1. Primera corrida y siguientes elecciones

Ejecuta desde la raíz del repositorio. La primera fase usa la fecha civil de Madrid y crea una corrida nueva. Una hora de fin anterior al inicio remite al día siguiente.

```bash
PY=.venv/bin/python
"$PY" prematch_steps.py phase1-run --start 15:00 --end 03:00 --confirm-external
```

La salida muestra la carpeta de la corrida y `ranking_fase1.md`. Para recuperar la primera corrida de la fecha después de cerrar la terminal, usa el selector explícito. `--show-run` devuelve solo la ruta. Para corridas guardadas fuera de `outputs/discovery/`, usa su `--eval-dir`.

```bash
RUN=$("$PY" prematch_steps.py phase1-list --first-run --show-run)
"$PY" prematch_steps.py phase1-list --eval-dir "$RUN" --exclude-fixture-id "$FID"
"$PY" prematch_steps.py phase1-list --eval-dir "$RUN" --min-lead 60 \
  --exclude-fixture-id "$FID" --exclude-fixture-id "$SECOND_FID"
```

Se puede fijar la fecha Madrid con `--first-run --date YYYY-MM-DD`. La fecha corresponde al inicio de la ventana original, también cuando esta cruza medianoche. El listado usa la huella del histórico enlazada por el ranking y la reconciliación Bwin local de esa corrida. `segunda_eleccion.md` conserva el puesto estadístico y muestra las mismas filas por hora de inicio. Indica ventana original, consulta en Europe/Madrid, margen y exclusiones por prioridad. Entre los elegibles deportivos cuenta primero la exclusión manual, después horario/estado, vínculo sin verificar y ausencia del ranking. Por defecto exige 120 minutos. La recuperación y el listado funcionan sin red.

Las etapas individuales siguen disponibles con el mismo `--eval-dir`.

| Etapa | Comando u opciones | Artefacto principal dentro del RUN |
|---|---|---|
| Descubrimiento | `discover --date YYYY-MM-DD --start 15:00 --end 03:00` | `fixtures.json` con ventana y auditoría |
| Filtro deportivo | `filter --policy top-tier-and-senior-cups` | `fixtures_major.json` y `league_filter_review.json` |
| Identidad Bwin | `bwin-scan --confirm-external`, o `bwin-scan --no-browser` para solo reconciliar lo guardado | `prospective_trial/bwin/bwin_discovery.json` y `bwin_scan_reconciliation.json` |
| Cola Bwin | `bwin-list --include-verified` | `prospective_trial/bwin/pending_manual_bwin_queue.json` |
| Histórico | `stats-fetch`, o `stats-fetch --fixture-id "$FID"` para un objetivo | `prospective_trial/enriched/preselection.json` |
| Patrones | `patterns` | `prospective_trial/tactical/tactical_signal_engine.json` |
| Estado | `status` | `steps/status.md` |

## 2. Fixture omitido o reclasificado

La primera corrida intenta vincular automáticamente los fixtures. La vía manual se usa para los cruces que quedan pendientes. Trabaja siempre sobre el mismo `RUN`.

Para un bloque de clubes ya elegibles, `bwin-bind` registra los pares fixture–URL. En zsh puedes guardar sus argumentos `--fixture-id ID` en un array y pasarlo como `"${fixtures[@]}"` a `bwin-verify` y `stats-fetch`. Ejecuta las etapas en este orden.

1. `bwin-verify --confirm-external` comprueba en Bwin los vínculos del bloque.
2. `bwin-list --include-verified` actualiza `steps/bwin_list.md` con los estados de vinculación.
3. `stats-fetch` añade el histórico ausente de los fixtures verificados del bloque con margen suficiente. Con el mismo filtro conserva el histórico existente.
4. `patterns` recalcula el ranking con el histórico actualizado.
5. `phase1-list --min-lead 45` publica en `segunda_eleccion.md` la lista con vínculo verificado, puesto en el ranking y al menos 45 minutos de margen.

```bash
"$PY" prematch_steps.py fixture-update --eval-dir "$RUN" --fixture-id "$FID"
"$PY" prematch_steps.py fixture-update --eval-dir "$RUN" --fixture-id "$FID" --apply
```

La primera llamada diagnostica sin escribir. Muestra clasificación actual y propuesta, identidad Bwin y Event ID observado, presencia en el histórico, divergencia con el filtro y presencia en el ranking. `--apply` activa las correcciones locales. Si cambia la clasificación, conserva la versión anterior del filtro y actualiza solo la fila objetivo. Los vínculos verificados se reutilizan. Un Event ID observado con hora sin acreditar queda pendiente de verificación. Cada acción consulta su propio margen.

Si la cola contiene un vínculo pendiente y se conoce el Event ID observado, se puede resolver la URL real desde el catálogo local y verificarla explícitamente.

```bash
"$PY" prematch_steps.py bwin-bind --eval-dir "$RUN" --fixture-id "$FID" --event-id "$EID"
"$PY" prematch_steps.py bwin-verify --eval-dir "$RUN" --fixture-id "$FID" --confirm-external
"$PY" prematch_steps.py fixture-update --eval-dir "$RUN" --fixture-id "$FID" --apply --confirm-external
```

`--confirm-external` con `fixture-update` permite intentar el escaneo individual y adquirir únicamente el histórico ausente del fixture. Cuando cambia la huella del filtro, se comprueban la procedencia del histórico en ese RUN y la identidad completa de cada fila reutilizada. Se guarda el histórico anterior con su huella y se registra la huella de origen de cada fila conservada. Una vez incorporado el fixture, el ranking se regenera localmente y la versión anterior se archiva. Si solo falta la fila del ranking y el histórico sigue vigente, basta la regeneración local.

## 3. Selección, captura y expediente base

### Pasada comercial por hora y cierre de prefinalistas

Usa el mismo `RUN` con vínculos Bwin verificados e histórico. `--hour 14:00`
abarca los kickoffs de 14:00 a 15:59 en Europe/Madrid, dentro de la ventana
original. La fecha se obtiene del RUN. Si la hora se repite en una ventana de
varios días, indica `--date YYYY-MM-DD`. Si un fixture verificado aún carece de
histórico, la pasada lo deja identificado. Añade ese histórico con
`stats-fetch --eval-dir "$RUN" --fixture-id "$FID"` y actualiza `patterns`.

```bash
"$PY" prematch_steps.py hour-batch-prepare --eval-dir "$RUN" --hour 14:00 --confirm-external
```

`--hour` crea un tramo de dos horas civiles hasta las 22:59. Los lotes que
empiezan a las 23:00, 00:00, 01:00 o 02:00 cubren el resto de la ventana
original hasta las 03:00, con límite final exclusivo. Esto permite iniciar una
sola pasada nocturna a las 23:00 y capturar todos los partidos restantes.

- `--hour 14:00`: 14:00–15:59.
- `--hour 16:00`: 16:00–17:59.
- `--hour 21:00`: 21:00–22:59.
- `--hour 22:00`: 22:00–23:59.
- `--hour 23:00`: 23:00–02:59.
- `--hour 00:00`: 00:00–02:59 del día civil siguiente.
- `--hour 01:00`: 01:00–02:59.
- `--hour 02:00`: 02:00–02:59.

El paquete registra `hour_madrid` y `batch_end_madrid`. Al cruzar medianoche,
la fecha se resuelve desde la ventana del RUN; usa `--date` únicamente si una
ventana excepcional hace ambigua la misma hora civil.

Esta acción toma una captura por fixture con margen suficiente, conserva los
manifiestos y continúa si un evento falla. Una nueva ejecución reutiliza las
capturas válidas y vuelve a intentar los pendientes. `--confirm-external`
autoriza la adquisición Bwin. Si se omite, la pasada usa las capturas existentes.
El directorio `hour_batches/<fecha_hora>/packets/<id>/` guarda el inventario de
cada evento, el cruce comercial y el Markdown `encargo_analista_codex_argon.md`. El
ranking estadístico de fase 1 sigue disponible por separado.

Si el lote incluye capturas anteriores al cambio de navegación de Bwin, usa
`hour-batch-prepare --eval-dir "$RUN" --hour 14:00 --confirm-external --refresh-todo`.
Se recapturan las que carezcan de confirmación de **Todo** en el DOM y las
caducadas, siempre con el margen habitual. Los partidos que sigan sin
confirmación quedan marcados como cobertura limitada. El nuevo encargo pide
inventariar todas las familias de los catálogos y reservar la lista de
prefinalistas para opciones con un cruce verificable.

Para Gemini en web, añade `--export-gemini`. Abre `encargo_gemini.md` y
adjunta `mercados_gemini.json`. Este inventario contiene todas las selecciones
capturadas y la evidencia resumida del lote. Guarda el bloque JSON de
prefinalistas que devuelva Gemini en `prefinalistas_json`. En un agente con
acceso al proyecto basta `instruccion_analista_cuotas.txt`.

Para volver a capturar y preparar solo un partido, ejecuta primero
`odds-capture --eval-dir "$RUN" --fixture-id "$FID" --confirm-external` y
después `hour-batch-prepare --eval-dir "$RUN" --hour 14:00 --fixture-id "$FID"`.
El nuevo paquete contiene solo el objetivo. Usa su propio encargo y su propio
`prefinalistas.json`. El scraper abre la URL de evento con `market=-1` y
comprueba la categoría **Todo** en la interfaz cuando puede identificarla.
El diagnóstico distingue la categoría activa en el DOM de la vista solicitada
por URL. La captura vuelve a buscar «Ambos equipos recibirán X o
más tarjetas» en la categoría **Tarjetas** si falta en la extracción general.
El diagnóstico del scraper indica dónde se encontró o si quedó sin extraer.
Las tarjetas con liquidación pendiente pueden incluirse en prefinalistas como
exploratorias. Sus amarillas históricas se mantienen separadas de la
liquidación comercial y el cierre conserva `LIQUIDACION_PENDIENTE` para
contratos de este tipo.

El lote también cruza props de jugador con el detalle histórico de API-Football
que ya esté registrado en la corrida. Busca identidad inequívoca por nombre
normalizado en el histórico de los dos clubes y conserva el ID de API-Football.
Para tiros, tiros a puerta, faltas cometidas, asistencias, gol o asistencia y
amarillas como referencia de tarjetas, exige tres apariciones y 90 minutos con
la métrica observada en la misma liga y temporada. `player_prop_coverage`
indica qué quedó en el inventario y por qué. El lote y el export de Gemini
incluyen las muestras enlazadas con sus minutos, rol histórico y recuento para
la línea exacta. El agente analiza el encaje con el rival y deja una condición
de XI y rol. El cierre coteja jugador y equipo por ID con el XI disponible y
conserva el estado de liquidación. Crack bwin y las variantes de goleador se
conservan en el inventario. El cruce reutiliza la caché referenciada y no pide
detalle adicional a API-Football.

Los tiros a portería colectivos usan la política comercial
`bwin-opta-on-target-v1`. La liquidación Bwin se registra con proveedor
`OPTA_STATS_PERFORM` a partir de la confirmación aportada por el usuario. Un
umbral `N+` se representa como `over (N−0,5)`. Por ejemplo, `4+` corresponde al
contrato `over 3,5`. El histórico de estimación sigue procediendo de
API-Football. El catálogo conserva ambos proveedores y marca pendiente la
reconciliación empírica de sus conteos. Una eventual corrección estadística
posterior al corte no reescribe el expediente PREMATCH y solo entra en la
auditoría posterior.

Abre Codex, Argon o Grok CLI en la raíz del repositorio con acceso de lectura
al RUN y permiso de escritura en la carpeta del lote. Pásale el contenido de
`instruccion_analista_cuotas.txt`, guardado junto a `encargo_analista_codex_argon.md`.
La misma instrucción queda al comienzo del encargo y en la última línea de
la salida del comando. El agente abre el
Markdown en disco, lee `lote.json` y consulta directamente los TXT de cuotas,
catálogos, cruces y fuentes estadísticas referenciadas. Deja el JSON en
`prefinalistas_json`, la ruta exacta que imprime el CLI. Puede escribir hasta
tres prefinalistas y un suplente, o una lista vacía. El encargo pide primero
la tesis favorable y sus condiciones, con el precio y la acreditación
probabilística por separado. Los datos sin calibración acreditada se usan
como diagnóstico exploratorio.

### Investigación cualitativa dinámica por finalista

Después de recibir `prefinalistas.json`, la investigación externa es opcional.
Para generar un encargo breve de Gemini para un partido finalista ejecuta:

```bash
PREFINALISTAS="/ruta/completa/al/paquete/prefinalistas.json"

"$PY" prematch_steps.py hour-batch-research --eval-dir "$RUN" --hour 14:00 \
  --shortlist "$PREFINALISTAS" --fixture-id "$FID" --provider gemini --research-mode focused
```

La variable debe contener la ruta completa del archivo JSON. Si está vacía,
el shell la convierte en el directorio actual y el CLI lo rechaza con
`HOUR_SHORTLIST_FILE_REQUIRED`.

Para limitarla a un partido añade `--fixture-id "$FID"`. El comando crea por
evento:

- `events/<FID>/research/requests/encargo_gemini_<EID>_focused.md`
- `events/<FID>/research/requests/insumos_gemini_<EID>_focused.json`
- `events/<FID>/research/responses/gemini_<EID>_focused.md`

El prompt incorpora identidad, kickoff, mercado, cuota capturada, tesis,
condiciones de XI, contradicciones, política de liquidación y preguntas
específicas. Adjunta el prompt y el JSON en Gemini. Copia su respuesta al
archivo de `responses`. Una reejecución actualiza el encargo y conserva la
respuesta existente. `focused` es el modo predeterminado. Pide una consulta
normal o búsqueda breve, hasta cuatro fuentes y solo preguntas que cambien
una tesis. `deep` conserva la búsqueda amplia opcional. `update` se concentra
en novedades desde el último informe aceptado o, si falta, en las últimas
24 horas. La interfaz del proveedor controla la duración. El CLI permite
generar encargos antes del kickoff sin esperar a un informe externo.

El lote prepara receptáculos vacíos o marcados para Gemini, Grok y Perplexity.
El cierre detecta sus respuestas e ignora los receptáculos pendientes y archivos
vacíos. Los informes deben traer `fixture_id`, `event_id`, fecha de generación
y un corte prepartido compatible. `auditoria_informes.json` registra aceptación,
rechazo, SHA-256, fechas declaradas, contradicciones y fuentes repetidas.

Si no hay tiempo para Gemini, omite `hour-batch-research`. La ruta rápida
continúa con Python, Shadow y la revisión final local. La ausencia de Gemini
reduce cobertura cualitativa y no decide por sí sola `NO BET`.

La primera ejecución de Codex o Argon sobre `encargo_analista_codex_argon.md` ya debe
clasificar cada prefinalista mediante `requires_confirmed_xi`. No hace falta
una segunda ejecución intermedia para crear una lista temprana. Los futuros
`prefinalistas.json` sirven para ambas modalidades. Las listas antiguas sin
ese campo quedan pendientes de XI por defecto.

```bash
"$PY" prematch_steps.py hour-batch-refresh --eval-dir "$RUN" --hour 14:00 \
  --shortlist "$PREFINALISTAS" --lineup-mode xi-confirmado --confirm-external
```

Para cerrar un único finalista del paquete añade `--fixture-id "$FID"`. Se
puede repetir la opción para varios partidos. Los restantes no se incluyen en
ese `cierre.json` y conservan su estado para una ejecución posterior.

`PREFINALISTAS` es la ruta al JSON que escribió el agente. El refresco revisa
solo los fixtures de esa lista. `xi-confirmado` es la modalidad predeterminada.
Dentro de T−75 consulta los XI por fixture a
API-Football cuando falta una alineación completa. Un XI estructurado del
proveedor conserva su procedencia. Si ambos XI están completos, busca una
captura reciente posterior a la primera. La
comparación muestra el cambio de cuota del mismo contrato. Si desaparece la
selección original, lista las líneas vecinas como alternativas por revisar.
`--confirm-external` autoriza tanto la consulta API como una nueva captura Bwin
si hace falta. En `xi-confirmado`, la consulta de XI y Shadow admiten T−5;
una captura del mismo contrato posterior a la inicial y con hasta 20 minutos
de edad puede preceder al XI. Se verifica su identidad, hash y precio, y se
registra el orden temporal. Si falta o caduca, se intenta una nueva captura
hasta T−5. El proceso completo debe terminar antes de T−5 y del kickoff.
Al repetir el refresco se reutilizan los XI completos ya registrados y la
segunda captura si conserva su vigencia.

Si API-Football todavía no publica los XI y hay capturas de un proveedor
externo que muestran «alineaciones confirmadas», prepara un JSON estructurado
con 11 titulares por equipo, fixture y Event ID, URL, hora de recepción,
capturas y sus SHA-256. Para un solo fixture usa
`--manual-lineups-json outputs/matches/<EID>/lineups/manual/manual_lineup_<EID>.json`
junto a `--lineup-mode xi-confirmado --fixture-id <FID>`. El refresco valida
identidad, hora anterior al kickoff y hash de cada imagen, registra la fuente
como `LINEUPS_EXTERNAL_CONFIRMED` y evita consultar API-Football. La hora de
recepción no demuestra la hora de publicación del proveedor; los nombres sin
ID quedan como transcripción visual. Una captura Bwin reciente del mismo
contrato puede preceder a los XI, con vigencia máxima de 20 minutos.
La captura inicial del lote exige T−45. En `sin-xi`, la captura de cierre y
Shadow requieren T−30. En `xi-confirmado` el margen es T−5. Al agotarse el
margen se registra un cierre operativo. Una captura anterior sigue disponible
para análisis y auditoría, con precio pendiente de verificación.

Para revisar el mismo paquete sin esperar los XI, ejecuta:

```bash
"$PY" prematch_steps.py hour-batch-refresh --eval-dir "$RUN" --hour 14:00 \
  --shortlist "$PREFINALISTAS" --lineup-mode sin-xi --confirm-external
```

`sin-xi` permite cerrar mercados colectivos con `requires_confirmed_xi: false`
en el candidato. `lineup_condition` debe explicar por qué la tesis se sostiene
con los escenarios plausibles. Los props, los candidatos dependientes del XI y
las listas antiguas sin ese booleano quedan pendientes de XI. Shadow evalúa
solo los contratos habilitados para ese cierre y aplica sus controles de
calibración, muestra y precio. La segunda captura tiene que ser posterior a la
primera y también al XI si ya hay uno completo registrado en `sin-xi`. En
`xi-confirmado` puede preceder al XI si sigue vigente. Cada ejecución guarda
su modalidad en `cierre.json` y genera un encargo final independiente. Puedes
ejecutar `xi-confirmado` después con el mismo `PREFINALISTAS`.

Si el paquete contiene varios partidos y no sabes cuáles tienen ya los dos XI,
usa la modalidad automática:

```bash
"$PY" prematch_steps.py hour-batch-refresh --eval-dir "$RUN" --hour 22:00 \
  --shortlist "$PREFINALISTAS" --fixture-id 1492466 --fixture-id 1639807 \
  --lineup-mode auto --confirm-external
```

`auto` consulta cada fixture de forma independiente dentro de la ventana de XI.
Cuando acredita 11+11 aplica `xi-confirmado` y admite sus candidatos; cuando no,
aplica `sin-xi` y conserva únicamente candidatos colectivos con
`requires_confirmed_xi: false`. La terminal imprime `modo=xi-confirmado` o
`modo=sin-xi` para cada fixture y `estado.json` registra
`requested_lineup_mode` y `effective_lineup_mode`. Un XI ausente o un fallo
aislado de su consulta no bloquea los otros partidos del comando.

Cuando `sin-xi` devuelve `LISTO_REVISION_FINAL`, pasa al agente el contenido
de `instruccion_cierre_llm_<FID>.txt` del fixture concreto. El refresco indica
las siguientes rutas por partido:

- `refresh/<REFRESH_ID>/<FID>/revision_final_codex_argon.md`
- `refresh/<REFRESH_ID>/<FID>/instruccion_cierre_llm_<FID>.txt`
- `refresh/<REFRESH_ID>/<FID>/resultado_cierre_llm.md` (destino del informe que escribe el agente)

La instrucción limita al agente al Fixture ID y Event ID del directorio y
detecta solamente los informes cualitativos de ese evento. Así, un refresco
con varios partidos no mezcla Gemini, cuotas, XI ni decisiones. La ejecución
local realiza el razonamiento final `BET / NO BET` temprano. Cuando
`xi-confirmado` alcanza el mismo estado, el nuevo directorio de refresco crea
otro encargo individual para el cierre con alineaciones. Una decisión temprana
ya ejecutada no se duplica como apuesta nueva al repetir el cierre con XI. La
segunda ejecución sirve para candidatos pendientes o para auditar el cambio de
tesis y precio.

- **T−45** es el margen mínimo para iniciar una captura desde el lote.
- **T−30** es el margen del modelo para `sin-xi` y para `model` independiente.
  El cierre integrado `xi-confirmado` admite T−5. La segunda captura tiene
  una vigencia máxima de **20 minutos** para el cierre del lote.
- Si un XI o el precio siguen pendientes, consulta el estado impreso y repite
  el refresco mientras quede margen. Si el tiempo se agota, el cierre registra
  el bloqueo operativo por separado de la decisión del modelo.
- Si aparece un pendiente técnico, el encargo permite registrar la causa y
  revisar la tesis con la evidencia disponible. La falta de una captura,
  liquidación o modelo aplicable no constituye por sí sola un `NO BET`.

## 4. Entrega al analista final

En `LISTO_REVISION_FINAL`, usa `instruccion_cierre_llm_<FID>.txt`, anunciado como
`archivo_instruccion_cierre_llm` en la terminal, en
ese refresco **dentro de la carpeta del fixture**. El agente lee el paquete y
escribe `resultado_cierre_llm.md` en la ruta indicada. Los cierres antiguos
con `instruccion_agente_local.txt` siguen siendo compatibles en auditoría.
El agente formula su tesis y verifica el contrato, muestra y precio antes de
leer Shadow. Cada candidato indica si Shadow lo evaluó y, si no, el motivo.
Si un informe
cualitativo termina después de generar el refresco, repite el mismo comando
`hour-batch-refresh` para crear un encargo nuevo que lo detecte automáticamente;
no edites a mano el prompt anterior.

## 5. Márgenes efectivos, cierre y auditoría

Los márgenes son inclusivos en los límites positivos y el kickoff cierra todas las acciones operativas. `audit-event` sigue disponible. Los mínimos del listado son independientes de los de adquisición o modelado.

| Acción | Margen efectivo en minutos |
|---|---|
| Listado de primera elección | 45 por defecto |
| Listado de segunda y siguientes | 120 por defecto. `--min-lead` cambia el listado |
| Selección y recuperación local de ranking para el objetivo | Antes del kickoff |
| Bwin scan, histórico nuevo y captura de cuotas | 45 por defecto. `fixture-update` exige 45 para sus llamadas externas |
| Bwin bind y Bwin verify | 5 por defecto |
| Cierre del lote `sin-xi` | 30 |
| Cierre del lote `xi-confirmado` | 5 |
| Auditoría | Disponible también después del inicio |

`model --min-lead 5` es un argumento inválido. Un rechazo por tiempo registra causa, margen, kickoff y acciones aún disponibles en `outputs/matches/<EID>/operations/`, separado de `model/model_report_<EID>.json`. El resultado de una operación bloqueada carece de decisión del modelo. Los ensayos con `--asof` usan hora ISO con offset y se identifican como replay en los artefactos de decisión. Usa el reloj real para operar y para cualquier adquisición externa en `fixture-update` u `odds-capture`.

```bash
"$PY" prematch_steps.py audit-event --eval-dir "$RUN" --fixture-id "$FID"
"$PY" prematch_steps.py audit-event --eval-dir "$RUN" --fixture-id "$FID" \
  --asof 2026-11-22T00:02:00+01:00 --output-dir "$AUDIT_DIR"
```

El inventario por evento entrega archivos sueltos con rutas, SHA-256 en JSON, faltantes y alertas. En una auditoría, el corte queda acotado al kickoff. El inventario no reejecuta el modelo y se escribe como archivo nuevo. Para acreditar disponibilidad propia antes del inicio, ejecuta `audit-event` con reloj real cuando el análisis exista. La auditoría posterior coteja su huella con el registro. Los replays y las revisiones designadas como auditoría se marcan como reconstrucción o revisión retrospectiva. Un archivo preexistente sin registro verificable conserva esa incertidumbre.

## 6. Diagnóstico reproducido del 21–22 de noviembre de 2026

| Corrida o fixture | Evidencia local y corrección |
|---|---|
| `prueba_bwin_20261121_014321` | Ventana 21/11 15:00 a 22/11 03:00 Madrid. Es la primera corrida recuperada para el 21/11. |
| `prueba_noche_20261121_193430` | Ventana 21/11 19:34–23:59 Madrid. Es una corrida distinta. La ventana termina antes del kickoff de Miami. |
| `1647047` Atlético Mineiro–Boca Juniors | En ambos filtros guardados figura elegible. Ambos históricos apuntan a una versión anterior del filtro. El fixture falta en los dos históricos y rankings, aunque cada ranking coincide con su propio histórico. En la primera corrida consta un vínculo manual pendiente y en la segunda consta el evento observado sin vinculación verificada. Regenerar el listado era insuficiente. Primero verificar el vínculo, después adquirir el histórico y actualizar el ranking según el margen de cada operación. |
| `1649495` Inter Miami–Columbus Crew | El manifiesto de la primera corrida resuelve `cuotas_bwin_7912617_20261121T221211Z.txt`. El TXT dice 23:12:28 Madrid y su huella se comprobó. La copia canónica y el catálogo previo corresponden a la captura de la tarde. Pedir una ruta fija del scraper fue un error de la instrucción operativa. La dependencia de la ruta manual en `market-catalog` y el uso por el modelo de la copia canónica eran defectos del flujo. |
| Márgenes | La sugerencia `model --min-lead 5` era inválida en la práctica. El CLI ocultaba el suelo aplicando un máximo interno. Ahora lo valida al recibir los argumentos. |
