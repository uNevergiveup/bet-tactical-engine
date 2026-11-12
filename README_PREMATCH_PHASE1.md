# PREMATCH Fase 1 — Data Foundation

Este orquestador construye la base de datos previa al análisis sin ejecutar
motores predictivos, señales, picks ni IA cualitativa. La red está desactivada
por defecto y cada llamada nueva a API-Football requiere `--online` y un
presupuesto positivo explícito.

## Flujo

1. Descubrimiento ligero de fixtures para la ventana 15:00–03:00
   `Europe/Madrid` (o reutilización de `fixtures.json`).
2. Clasificación adulta abierta: masculino, femenino y género desconocido;
   juveniles/reservas identificables se excluyen y las dudas quedan en REVIEW.
3. Un único catálogo Bwin público por ciclo. Solo se usa el DOM público mediante
   Playwright; no se consultan APIs privadas.
4. Dos estados independientes: `api_verified` exige Fixture ID, equipos/localía,
   competición, temporada, género y kickoff API futuro; `verified` añade la unión
   Bwin estricta. Un countdown, una coincidencia nominal o la ausencia en páginas
   escaneadas no bastan para esta última.
5. Adquisición profunda cache-first, limitada por presupuesto y reanudable. El
   piloto prioriza historial y solicita estadísticas, jugadores, lineups y eventos
   antes del contexto opcional.

## Comandos

Auditoría offline de una ventana ya descubierta (cero red):

```bash
.venv/bin/python prematch_phase1.py --date 2026-11-07 --offline --max-api-calls 0
```

Descubrimiento de una jornada nueva: obtiene como máximo los dos días API que
cruza la ventana y realiza un solo barrido Bwin compartido. No profundiza:

```bash
.venv/bin/python prematch_phase1.py --date YYYY-MM-DD --online --discover-bwin \
  --discovery-only --max-api-calls 2 --bwin-headless
```

Piloto online de hasta tres fixtures API verificables y como máximo diez llamadas
nuevas. Reutiliza el catálogo generado por el comando anterior; Bwin sigue siendo
un requisito separado para mercados:

```bash
.venv/bin/python prematch_phase1.py --date YYYY-MM-DD --online \
  --max-matches 3 --history 5 --max-api-calls 10 --bwin-headless
```

Piloto API explícito para tres Fixture IDs, con límite duro de 15 llamadas
nuevas. La lista es un ejemplo reproducible de la ventana del 7 de noviembre;
el programa rechaza IDs que no superen la verificación API independiente:

```bash
.venv/bin/python prematch_phase1.py --date 2026-11-07 --online --resume \
  --fixture-id 1526023 --fixture-id 1530034 --fixture-id 1548557 \
  --history 5 --max-api-calls 15
```

Repetición estrictamente offline de ese piloto: reutiliza la caché y registra
cualquier hueco sin hacer llamadas nuevas:

```bash
.venv/bin/python prematch_phase1.py --date YYYY-MM-DD --offline \
  --max-matches 3 --history 5 --max-api-calls 0 --resume
```

Repetición offline de esos mismos tres IDs:

```bash
.venv/bin/python prematch_phase1.py --date 2026-11-07 --offline --resume \
  --fixture-id 1526023 --fixture-id 1530034 --fixture-id 1548557 \
  --history 5 --max-api-calls 0
```

Auditoría legible de cobertura ya generada:

```bash
.venv/bin/python -m json.tool outputs/phase1/YYYY-MM-DD_1500_0300/api_coverage.json
```

Ejecución completa con un presupuesto explícito de 200 llamadas nuevas como
máximo (se puede cambiar ese número conscientemente):

```bash
.venv/bin/python prematch_phase1.py --date YYYY-MM-DD --online --discover-bwin \
  --history 20 --max-api-calls 200 --bwin-headless --resume
```

`FOOTBALL_API_KEY` debe existir en el entorno para el modo online. No se imprime
ni se guarda en manifiestos. `--date` fija manualmente el día de inicio; `--start`
y `--end` permiten una ventana distinta.

## Salidas

Cada ejecución escribe en `outputs/phase1/<fecha>_1500_0300/`:

- `fixtures_discovered.json`: universo ligero, incluidos REVIEW y excluidos.
- `bwin_catalog.json`: eventos y páginas públicas observadas una vez por ciclo.
- `fixture_identity_map.json`: verificados, pendientes, no encontrados y excluidos.
- `api_coverage.json`: cobertura real por endpoint y métricas no nulas.
- `raw_manifest.json`: procedencia, SHA-256, caché y consumo de presupuesto.
- `phase1_progress.json`: punto de reanudación auditable.
- `phase1_summary.json` y `phase1_summary.md`: resumen operativo.
- `phase1_execution.log`: resumen y registro JSONL de cada operación API/caché.

La caché compartida está en `outputs/.api_football_history_cache`. La clave
incluye endpoint y parámetros normalizados. Una respuesta vacía es negativa y
caduca a los cinco minutos; una respuesta positiva usa el TTL documentado por
familia. JSON corrupto, hash incorrecto, identidad de clave incompatible y
timestamps futuros se ignoran y quedan registrados.

También existen adaptadores de solo lectura para las cachés históricas de
`generate_context.py` (`outputs/data/cache`) y `preselect_matches.py`
(`outputs/data/cache/preselect_api`). Solo se reutilizan si la clave, estructura,
fecha y TTL coinciden; esos archivos nunca se reescriben ni se eliminan.

## Restricciones temporales y de integridad

- Nunca se incluye el fixture objetivo ni un histórico posterior a su kickoff.
- Solo cuentan históricos en estado final; LIVE se excluye.
- `null` se conserva como ausencia; un cero observado sigue siendo evidencia.
- Datos adquiridos después del kickoff objetivo se etiquetan
  `RETROSPECTIVE_ONLY` y no representan disponibilidad PREMATCH original.
- Un evento Bwin ambiguo, invertido, juvenil/reserva o con hora no verificable no
  activa la adquisición profunda.

La aceptación offline valida estructura, caché y seguridad. El estado final de
esta fase debe permanecer `FASE_1_READY_FOR_TEST` hasta completar el piloto
online controlado; no equivale a una fase productiva ni a autorización de apuesta.
