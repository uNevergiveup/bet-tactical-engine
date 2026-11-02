# ENCARGO A CODEX — CSV ÚNICO, LIMPIEZA CONTROLADA Y VENTANA 15:00–03:00

## Contexto
Proyecto local: `/Users/antonio/Projects/scraper` (Python/Playwright, API-Football Pro, Bwin). El flujo PREMATCH implementado incluye `prematch_daily.py`, `prematch_prospective_trial.py`, descubrimiento/filtros, `preselect_matches.py`, `prematch_pattern_engine.py`, `bwin_discovery.py`, `bwin_market_preflight.py`, `prematch_opportunity_scanner.py`, validadores y motor. El último cambio `window_scope_v2` informa 125 pruebas superadas, pero no hubo una nueva ejecución real de descubrimiento y captura. No supongas que todo está correcto: inspecciona código, pruebas, config, salidas y git antes de modificar.

**Objetivos prioritarios:** (1) un CSV consolidado con TODOS los encuentros descubiertos y trazabilidad por fase; (2) ventana operativa predeterminada de 15:00 a 03:00 Europe/Madrid (hasta el día siguiente), con perfiles opcionales; (3) eliminar archivos y caminos de ejecución realmente obsoletos sin perder evidencia ni romper módulos; (4) ejecutar una prueba prospectiva real y auditable en toda esa ventana, con control de coste.

## A. CSV único diario, sin pérdida de candidatos

Genera/actualiza continuamente:
`outputs/discovery/<YYYY-MM-DD_1500_0300>/prospective_trial/partidos_diarios.csv`

**Una fila por `api_fixture_id` descubierto en la ventana**, incluido si luego fue excluido. No generes solamente filas de seleccionados ni de `top 5`. Conserva también `partidos_diarios.json` si facilita trazabilidad y `partidos_diarios.md` como resumen legible, pero CSV será la fuente de revisión humana.

Campos mínimos:
- `fecha_ventana`, `inicio_ventana_madrid`, `fin_ventana_madrid`, `fixture_id`, `kickoff_madrid` (ISO con offset), `pais`, `liga`, `league_id`, `temporada`, `local`, `visitante`.
- `liga_elegible` (true/false/unknown), `motivo_filtro_liga`, `prematch_elegible` (true/false), `minutos_hasta_kickoff_en_captura`.
- `fase_maxima`, `estado_final`, `motivo_exclusion_o_pendiente`, `detalle_evidencia` (no llenar con una suposición).
- `preselection_evaluada` (true/false), `preselection_score`, `cobertura_api`, `patrones_detectados`, `patrones_resumen`.
- `bwin_busqueda_estado`, `bwin_event_id`, `bwin_url`, `identidad_bwin_estado`, `evidencia_horaria_estado`, `cuotas_capturadas`, `captura_madrid`, `sha256_snapshot`, `cuotas_elegibles`, `mercados_investigables`.
- `opportunity_evaluado` (true/false), `opportunity_estado`, `mejor_hipotesis_mercado`, `mejor_hipotesis_cuota`, `score_oportunidad` (solo si se calcula de verdad), `seleccionado` (true/false), `razon_seleccion_o_no_seleccion`, `actualizado_madrid`.

**Semántica estricta:** distingue `DESCARTADO_POR_REGLA`, `BLOQUEADO_IDENTIDAD`, `BLOQUEADO_TIEMPO`, `SIN_COBERTURA`, `SIN_BWIN`, `SIN_CUOTAS`, `NO_EVALUADO_POR_LIMITE`, `PENDIENTE_ACTUALIZAR_CUOTAS`, `ANALIZADO_SIN_SENAL_VALIDADA`, `CANDIDATO_INVESTIGACION`, `SELECCIONADO`. No presentar `NO_EVALUADO` como NO BET. No inventes puntuaciones, odds ni valores ausentes: deja vacío o `N/D` según esquema y documenta la regla.

**Requisito fundamental:** el CSV se construye desde la lista completa de `fixtures.json` filtrada por ventana, y se enriquece incrementalmente mediante `fixture_id` con información de cada etapa. Si una etapa falla, la fila persiste. Ordena por kickoff y luego prioridad. Reutiliza el registro de trazabilidad existente si funciona; no crees dos fuentes contradictorias. CSV UTF-8-SIG o UTF-8 coherente con Excel ES (delimitador documentado), escritura atómica y deduplicación. Verifica que LAFC–Minnesota (`fixture_id=1490590`) no desaparece de una auditoría histórica por estar fuera del lote de preselección: debe aparecer con su razón real.

## B. Configuración diaria predeterminada

Centraliza la ventana en `config/prematch_windows.json` (o la configuración canónica existente), y consúmela desde ambos lanzadores y el descubridor sin duplicar valores:
- Inicio **15:00 Europe/Madrid**.
- Fin **03:00 del día siguiente**.
- `default_profile` que resuelva a `15:00–03:00`.
- Permitir `--date`, `--window`, `--start`, `--end` sin romper la ejecución diaria; perfila `normal` 15:00–03:00 y, si conviene, otros perfiles con nombres inequívocos.
- Entre las 00:00 y 03:00, el comando diario sin fecha debe reconocer la ventana que empezó el día anterior. A las 03:00 exactas define y prueba el límite; respeta horario CET/CEST y cambios de hora.
- **No confundas ventana diaria (12 h) con `--horizon-hours` (por defecto 3 h del selector operativo).** Para la prueba de cobertura completa usa explícitamente `--horizon-hours 0` si así está definida su semántica; verifica la semántica en código, no la asumas.
- Mantén los cierres: kickoff no pasado, mínimo margen de ejecución, identidad verificada, cuota final capturada dentro de los últimos 60 minutos antes del inicio y antigüedad máxima 15 min al consumirla. Una captura de mañana para un partido de tarde es preparación anticipada, no un pick listo.

## C. Limpieza del proyecto y eliminación de lo innecesario

1. Inspecciona `git status`, imports, referencias entre scripts, `AGENTS.md`, README, tests, CLI y entrypoints; inventaría duplicados, scripts obsoletos, ZIPs de iteraciones, logs efímeros y salidas de pruebas.
2. Prepara un **plan de eliminación** (`outputs/engineering/cleanup_.../cleanup_manifest.csv`) con ruta, tipo, motivo, tamaño, referencias halladas, destino, riesgo.
3. Puedes eliminar archivos temporales generados y duplicados verificablemente no usados, **solo** tras registrar el inventario y comprobar sus dependencias. Si existen dudas, conserva o archiva; no borres los únicos históricos necesarios para auditoría, `match_identity`, datos originales, fuente del pipeline, tests funcionales, `AGENTS.md`, prompts ni credenciales/configuraciones. Nunca imprimas secretos.
4. Simplifica interfaces redundantes pero mantén compatibilidad de comandos/documentación o incluye migración verificada. No uses `rm -rf` general sobre `outputs/` o `tests/`. En caso de archivos versionados, usar git de forma rastreable y mostrar diff. No eliminar sin respaldo lo que no puedas reproducir.
5. Deja una estructura de carpetas limpia y un `README` actualizado con **un único comando diario**, salida CSV e instrucciones para reabrir el historial.

## D. Prueba prospectiva real y suficientemente amplia

Ejecuta una nueva corrida para la **ventana operativa de hoy que corresponda a 15:00→03:00**, utilizando `Europe/Madrid`. Antes de gastar API, muestra `--dry-run` / `--show-config`, rango y presupuesto estimado. Si la ventana actual está caducada, elige la próxima futura y deja documentada la decisión.

1. Descubre **todos** los fixtures dentro del intervalo; registra cada uno en el CSV y filtra **solo primeras divisiones adultas masculinas o femeninas**, sin juveniles ni reservas.
2. No trunques la lista global por `valid[:5]`. Prioriza y procesa **lotes configurables**: como prueba, intenta evaluar estadísticamente ~10–15 candidatos, descubrir Bwin para hasta 10 y obtener **hasta 3 capturas verificadas**, sujeto a partidos realmente elegibles, margen temporal y disponibilidad. El límite es por **éxitos**, no por primeros intentos. Controla consumo API-Football y reutiliza caché válida por SHA. Ajusta números a coste real e informa las limitaciones, no simules resultados.
3. Si un candidato falla identidad, hora o precio, registra el motivo exacto y continúa con el siguiente. No saltarse las validaciones ni seleccionar automáticamente un encuentro no evaluado.
4. Ejecuta Pattern Engine, Preflight y Opportunity Scanner con **las mismas identidades y datos frescos**. Registra scores y mejores hipótesis, diferenciando señales descriptivas, modelos no calibrados y valor verificado. NO generar picks, stake ni EV validado si no hay modelo calibrado. No invocar Grok/Gemini ni Codex analista.
5. Concluye con CSV consolidado, tabla en consola, informe MD, handoff si existe un candidato listo, registros de API calls, capturas Bwin, resultados de pruebas y `git diff`. Si no hay candidatos suficientes, deja filas y causas; no inventes éxitos.
6. Corre suite de regresión, pruebas de cambio de fecha (incluida ventana anterior tras medianoche), preservación de filas no evaluadas, reintentos y semántica de cuotas antiguas.

## E. Entrega verificable

Proporciona las **rutas absolutas realmente creadas**, el número de partidos descubiertos, filtrados, evaluados estadísticamente, verificados Bwin, capturados, analizados y seleccionados, y los motivos de exclusión. Muestra 5–10 filas reales del CSV e indica dónde consultarlo. Explica qué eliminaste o archivaste y cómo revertirlo. No declarar completada una prueba real si solo se ejecutaron mocks; distingue pruebas offline e integración real.

**Comando diario deseado:** `python prematch_daily.py` sin parámetros, usando 15:00→03:00 como ventana por defecto. Para auditoría integral, permitir un comando explícito de ventana completa (`--horizon-hours 0` solo si está implementado con esa semántica).
