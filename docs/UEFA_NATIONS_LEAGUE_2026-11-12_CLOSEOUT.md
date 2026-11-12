# Cierre Objetivo 5.1 — UEFA Nations League, 12/11/2026

Auditoría de ocho fixtures usando snapshots locales de API-Football/estadística y una inspección Bwin dirigida. No se ejecutó investigación cualitativa, ampliación de patrones con IA, generación de picks ni adquisición histórica nueva.

## Criterios de estado

- `DATA_READY`: identidad Bwin verificada con equipo local/visitante, Event ID, competición, categoría y kickoff; además existen observaciones históricas locales para goles, córners, tarjetas, faltas y remates.
- `PARTIAL`: identidad válida, pero alguna familia o dimensión tiene cobertura incompleta, muestra insuficiente para el umbral del modelo o props de jugador no utilizables.
- `BLOCKED`: identidad o integridad temporal no verificable. Ningún fixture de esta lista queda bloqueado después de la inspección dirigida.

`DATA_READY` describe preparación del análisis estadístico colectivo. No significa modelo validado, cuota capturada, valor esperado ni estado comercial READY.

## Matriz de los ocho fixtures

| Fixture API | Partido | Event ID Bwin | Evidencia Bwin | Kickoff | Mercados/encabezados observados | Historial local | Estado |
|---:|---|---:|---|---|---|---|---|
| 1528958 | Türkiye–Belgium | 7905470 | título y URL coincidentes; UEFA Nations League; categoría sénior corroborada | 18:00 CET, diferencia 0 min | resultado, goles, córners, tarjetas, tiros, tiros a puerta, faltas | 64 observaciones por familia colectiva; xG 56/64 (87,5%); offsides 58/64 | DATA_READY |
| 1528959 | Armenia–Cyprus | 7909685 | título y URL coincidentes; UEFA Nations League; categoría sénior corroborada | 18:00 CET, diferencia 0 min | resultado, goles, córners, tiros a puerta; sin tarjetas/faltas observadas | 64 por goles/córners/tarjetas/faltas/remates; xG 56/64 | DATA_READY |
| 1528960 | Italy–France | 7905471 | título y URL coincidentes; competición y categoría corroboradas en página | 20:45 CET, diferencia 0 min | resultado, goles, córners, tarjetas, tiros, tiros a puerta, faltas | 64 por familias principales; xG 56/64 | DATA_READY |
| 1528961 | Czechia–Spain | 7905479 | título y URL coincidentes; competición y categoría corroboradas en página | 20:45 CET, diferencia 0 min | resultado, goles, córners, tarjetas, tiros, tiros a puerta, faltas | 64 por familias principales; xG 56/64 | DATA_READY |
| 1528962 | England–Croatia | 7905480 | título y URL coincidentes; competición y categoría corroboradas en página | 20:45 CET, diferencia 0 min | resultado, goles, córners, tarjetas, tiros, tiros a puerta, faltas | 64 por familias principales; xG 56/64 | DATA_READY |
| 1528963 | Albania–Finland | 7909679 | título y URL coincidentes; competición y categoría corroboradas en página | 20:45 CET, diferencia 0 min | resultado, goles, córners, tiros, tiros a puerta; sin tarjetas/faltas observadas | 64 por familias principales; xG 56/64 | DATA_READY |
| 1528964 | San Marino–Belarus | 7909680 | título `San Marino–Bielorrusia`, URL, UEFA Nations League y categoría sénior corroborados | 20:45 CET, diferencia 0 min | resultado, goles, córners, tiros a puerta; sin tarjetas/faltas observadas | 64 por familias principales; xG 56/64 | DATA_READY |
| 1545613 | Montenegro–Latvia | 7909686 | título `Montenegro–Letonia`, URL, UEFA Nations League y categoría sénior corroborados | 20:45 CET, diferencia 0 min | resultado, goles, córners, tiros a puerta; sin tarjetas/faltas observadas | 64 por familias principales; xG 56/64 | DATA_READY |

La inspección Bwin fue PREMATCH. Las cabeceras son indicadores de mercado, no prueba de precio seleccionable. No se capturaron cuotas nuevas.

## Cobertura estadística local

Fuente principal: `outputs/phase1/2026-11-12_1500_0300/statistics_all_history20_01/api_coverage.json`, `deep_progress.json` y artefactos de `outputs/control/runs/python_engine_real_v1/phase2/`.

- Para los ocho fixtures, las familias colectivas `goals`, `corners`, `fouls`, `yellow`, `shots` y `on_target` tienen 64/64 observaciones solicitadas válidas (100%).
- `xg` tiene 56/64 (87,5%) y `offsides` 58/64 (90,62%). No se imputan los campos ausentes.
- El detalle histórico por partido está disponible en eventos, alineaciones, jugadores y estadísticas para la mayoría de los 39 partidos consultados por fixture; la cobertura agregada de la liga es `fixtures/events` 2178 disponibles/260 vacíos, `lineups` 1705/733, `players` 834/1604 y `statistics` 1448/990.
- Los patrones phase2 muestran para cada fixture 16 registros `INSUFFICIENT_SAMPLE` y 4–6 `WEAK_PATTERN`. La razón dominante es `MIN_SAMPLE_NOT_MET`: los subconjuntos estrictos por localía/rival comparable suelen tener n=2, aunque el universo colectivo de la liga tenga 64 observaciones.
- Jugadores y props individuales quedan `PARTIAL`: la identidad de jugador, titularidad objetivo y exposición futura no están acreditadas antes del kickoff; los artefactos player-patterns se conservan como evidencia descriptiva, no como autorización de mercado.
- Periodos 1T/2T, entrenadores actuales, árbitro, PPDA y régimen táctico no se consideran completos en esta etapa. Ausencia de esos campos no se convierte en cero.

## Mercados conocidos y límites

Los ocho eventos muestran mercado de resultado y goles; todos muestran total de córners; algunos muestran tarjetas, faltas de jugador, tiros y tiros a puerta. Armenia, Albania, San Marino y Montenegro no mostraron tarjetas/faltas en la inspección; esto es `NO_OBSERVED`, no mercado inexistente. No hay snapshot de precios verificado para ninguno de los ocho en esta fase.

## Artefactos

- Reconciliación final de los 73 fixtures: `outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json`.
- Piloto dirigido San Marino/Montenegro: `outputs/phase1/2026-11-12_1500_0300/objective5_1_targeted_03/bwin_discovery.json`.
- Inspección dirigida Italia/Chequia/Inglaterra/Albania: `outputs/phase1/2026-11-12_1500_0300/objective5_1_targeted_04/bwin_discovery.json`.
- Historial y cobertura API: `outputs/phase1/2026-11-12_1500_0300/statistics_all_history20_01/api_coverage.json`.

## Conclusión

Los ocho fixtures quedan preparados (`DATA_READY`) para una etapa independiente de descubrimiento estadístico colectivo, con cobertura local suficiente de goles, córners, tarjetas, faltas y remates. Los resultados no son todavía modelos comerciales validados: persisten muestras estrictas pequeñas, campos xG/offsides ausentes, cobertura irregular de jugadores y ausencia de cuotas capturadas nuevas. No se han generado picks.
