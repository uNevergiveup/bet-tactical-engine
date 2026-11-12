# PREMATCH V2 — Fase 2: generalización e integración offline

Implementación del 9 de noviembre de 2026. Ninguna adquisición API/Bwin ni ejecución LLM.

## Cambios y causas verificadas

1. Ranking y selector tenían nombres 20261108 y universos 290/8021 constantes.
   Ahora el ranking deriva cantidades del snapshot, admite `--date`, `--start`,
   `--end` Europe/Madrid (por defecto 15–03, fin exclusivo) y comprueba opcionalmente
   `--preselection` por SHA contra el Predictor congelado. Si no se fuerza fecha,
   se infiere del primer kickoff del snapshot; para ventanas tras medianoche se
   recomienda indicar siempre la fecha operativa. No se vuelve a ejecutar el Predictor.
2. Duplicados exactos por pattern_id se cuentan una sola vez; conflictos de contenido
   con el mismo ID son error. Se conserva el número original y duplicados removidos
   en el manifiesto. La fórmula de puntuación permanece igual.
3. Ranking por partido/familia y nuevo CSV individual por pattern_id/métrica/equipo,
   con muestra, cobertura, contradicciones y fixtures de evidencia. La vista ofensiva
   agregada conserva su fórmula; declara cuántos patrones son goles y cuántos xG.
   En el individual ambos son inequívocos. xG no se vincula a cuotas de goles.
4. El selector lee la fecha del CSV/manifiesto, conserva todas las posiciones por
   familia, hace la unión TOP20/TOP10 sin recorte global y mantiene presupuesto de
   capturas separado. Rechaza fixtures repetidos, mezcla de universos por familia,
   offsets desconocidos y evaluación anterior al cutoff. Binding ausente permanece
   pendiente y no da permiso para scraping.
5. Mapping admitía H1/H2 para patrones FT porque `_compatible` solo comparaba
   familia/equipo. Ahora exige período explícito coincidente; UNKNOWN no equivale
   a FT. La política actual no transforma series ni introduce modelos nuevos.
6. Mapping confiaba en los precios del CSV derivado. Ahora reutiliza `snapshot_odds`
   y exige que cada triple del CSV exista exactamente en el TXT con SHA válido.
   Comprueba URL/fecha de cabecera y antigüedad de evidencia Bwin respecto a captura
   (máximo 180 minutos). Evento reutilizado entre fixtures se bloquea.
7. Si faltaba el CSV, el código guardaba la razón pero no invalidaba el enlace:
   ahora es un bloqueo explícito. Alterar CSV, metadatos o TXT impide el MATCHED.
8. Cuota mínima configurable con `--min-odd` en mapping y `--min-market-odd` en daily,
   default 1,20. Cada enlace conserva su precio y `price_eligible`, incluidos precios
   bajos para auditoría. Todos bajos ⇒ DISCARDED_CANDIDATE; evidencia insuficiente
   ⇒ PENDING_VALIDATION. Una cuota elegible no es valor esperado.

## Los 853 pendientes del día 9

Evidencia: `outputs/discovery/2026-11-09_1500_0300/prospective_trial/candidate_ledger.json`,
campos `fixtures[].state/reason/history`, más los cinco preflights originales de
`daily_runs/20261109T020353042168/preflight/`. El JSON de mapping incluye el SHA
del ledger y la historia de cada fixture pendiente; las causas no se infieren de
popularidad, liga ni ausencia de un archivo sin explicación.

| Causa final registrada (excluyentes) | Fixtures | Patrones |
|---|---:|---:|
| MAX_SCRAPES_BUDGET_NOT_ANALYZED | 34 | 707 |
| DISCOVERY_RETRIES_EXHAUSTED: NOT_FOUND_IN_SCANNED_PAGES | 6 | 146 |
| Total sin captura incorporada | 40 | 853 |

707 se clasifican CAPTURE_LIMIT_REACHED; 146 EVENT_NOT_FOUND_IN_SCANNED_PAGES.
Algunos diferidos tenían obstáculos de catálogo o identidad anteriores: su historia
se conserva y no se afirma que bastaría capturarlos para que fueran válidos.
Los seis no encontrados significan únicamente ausencia en las páginas exploradas.
No hay pendientes sin razón identificada en este universo. No se atribuye ninguno
a cuota no disponible, identidad pendiente o error de rutas como causa final
excluyente sin un registro que lo acredite. Si no se aporta ledger, el sistema
usa NO_CAPTURE_EVIDENCE_UNAVAILABLE; no inventa causa.

## Estados y contrato exacto

Cada asociación lleva pattern_id, fuente/SHA/versión, fixture_id/event_id, team_id
o player_id (null cuando no existe), métrica, período, mercado/selección literal,
línea/dirección, precio, captura/fecha/SHA, CSV/SHA, evidencia horaria y estado de
identidad. Los modelos de jugadores no se implementan en esta fase.

* PATTERN_DISCOVERED_PENDING_CAPTURE / PENDING_ODDS: falta preflight, razón acreditada.
* MARKET_NOT_OBSERVED / MARKET_NOT_AVAILABLE: no apareció la línea por equipo en
  el alcance de esa captura. ONLY_INCOMPATIBLE_PERIOD_OBSERVED explica H1/H2.
* MARKET_OBSERVED_WITHOUT_USABLE_PRICE: encabezado compatible sin precio válido.
* MARKET_LINKED / MARKET_MATCHED: evidencia descriptiva respaldada y al menos una
  cuota >= mínimo, bajo contrato V1 endurecido.
* PENDING_VALIDATION: enlaces presentes, modelo descriptivo no respaldado.
* DISCARDED_CANDIDATE: precios observados, todos por debajo del mínimo.
* IDENTITY_BLOCKED: integridad o unión incorrecta.
* UNSUPPORTED_MARKET: xG, offsides y otras métricas sin modelo comercial compatible.

Offsides queda disponible en ranking táctico, con commercial_model_supported=false.
MARKET_MATCHED cuenta patrones con enlaces, no número de apuestas. Se conservan
ambas direcciones reales de las líneas; el enlace no representa una predicción Over/Under.
La liquidación se marca BWIN_RULES_REVIEW_REQUIRED: nunca se inventan reglas oficiales.
Identidad corroborada por preflight no se promociona a hora absoluta machine-verified.

## Ejecuciones congeladas y ejemplos

Salidas nuevas en `outputs/phase3/v2_phase2_final/{20261108,20261109}/`:
`ranking/` y `candidates/`. Contienen CSV general, siete familiares, individual,
manifiesto y candidatos CSV/JSON/Markdown. Evaluación del selector fijada al cutoff
original, no al reloj actual: son auditorías del snapshot, no disponibilidad actual.

| Día | Fixtures | Patrones | Candidatos únicos |
|---|---:|---:|---:|
| 8 | 290 | 8021 | 31 |
| 9 | 45 | 1048 | 20 |

TOP3 del día 9: FC Koln W–RB Leipzig W (1593167; 76,784), Sportivo Luqueno–2 de Mayo
(1632060; 75,2659), Club Sp. San Lorenzo–Rubio NU (1632059; 73,9676).
Las puntuaciones son EVIDENCE_PRIORITY_UNCALIBRATED.

Mapping real día 9:
`outputs/discovery/2026-11-09_1500_0300/prospective_trial/market_mapping_v1/runs/f74402429e8e339e8d74/`.
1.048 patrones = 4 MATCHED + 8 PENDING_VALIDATION + 123 MARKET_NOT_AVAILABLE +
60 UNSUPPORTED + 853 PENDING_ODDS; ningún IDENTITY_BLOCKED. Las asociaciones
se restringen ahora a FT: 93 enlaces (incluidos repetidos legítimos por patrones
distintos), 75 con precio >=1,20 y 18 descartes comerciales por precio.

Ejemplo literal comprobado contra TXT: fixture 1549860 / Event 7907189, patrón
`1549860:home:goals:EXPLICIT:CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE`, Junior,
`CD Junior FC Barranquilla - Total de goles | Tiempo reglamentario`, `Más de 1,5`,
línea 1,5 OVER, cuota 1,55, captura `2026-11-09T02:08:08+01:00`, SHA original
`28dd815a3dbf0e3e9eb047dfb4e5fe6b2ddf7e4691b8bd5ce635997e5aa62b12`.
La cuota 1,10 del mismo equipo queda conservada como DISCARDED_BELOW_MIN_ODD.

Reproceso día 8 con la captura archivada Atlético–Barcelona:
`outputs/phase3/v2_phase2_final/20261108/mapping/`: 2 patrones MATCHED; los
enlaces H1/H2 antes incluidos ya no se vinculan con FT.

## Reproducción, sin adquisición

Desde `/Users/antonio/Projects/scraper`:

```bash
.venv/bin/python prematch_daily.py --date 2026-11-09 --market-mapping-only --min-market-odd 1.20

.venv/bin/python prematch_predictive_evidence_ranking_v1.py \
  --patterns outputs/discovery/2026-11-09_1500_0300/prospective_trial/predictive/1.0-mvp/20261109T012758+0100_ada40f30a2ac_code-810f03a2ea4f/predictive_patterns.json \
  --preselection outputs/discovery/2026-11-09_1500_0300/prospective_trial/enriched/preselection.json \
  --date 2026-11-09 --start 15:00 --end 03:00 \
  --output-dir outputs/phase3/v2_phase2_final/20261109/ranking

.venv/bin/python prematch_candidate_selector_v1.py \
  --ranking-dir outputs/phase3/v2_phase2_final/20261109/ranking \
  --asof 2026-11-09T00:27:58.199511+00:00 --max-bwin-captures 5 \
  --output-dir outputs/phase3/v2_phase2_final/20261109/candidates
```

Un snapshot de otra fecha requiere sus rutas y fecha operativa; no hay IDs ni
directorios diarios fijos en el código. Cambios de entradas/parámetros usan otra
carpeta: las escrituras derivadas write-once rechazan reemplazos incompatibles.
Cada CLI indica salida y siguiente acción. No ejecuta Grok/Perplexity ni captura.
Cuotas frescas e identidad son obligatorias antes del uso actual; XI oficial si
la tesis lo requiere; investigación externa es opcional según evidencia faltante.

## Validación y pendientes

Suite completa: 231 PASS. Nuevas regresiones en `test_prematch_v2_phase2.py`:
H1/H2 incompatibles, CSV manipulado, precio mínimo/inclusivo, trazabilidad, xG/offsides,
853 pendientes exactos, generalización días 8/9, deduplicación/conflictos,
fecha adicional con CEST, mercado sin precio, reutilización incorrecta de Event ID,
evidencia caducada, TXT alterado y rechazo explícito de eventos/mercados LIVE. Socket.connect bloqueado en los tests nuevos y
en la integración real offline. Validado que seleccionar antes del cutoff falla.

Idempotencia: 30 salidas de integración idénticas al repetir; 16 entradas históricas
(incluidos Predictor, preflights, TXT, ledger y mapping previo) conservaron SHA.
Los rankings/candidatos finales se repitieron con 13 salidas idénticas por jornada.
Sin cambios en Predictor V1, detector estadístico, SOS ni fórmulas de ranking.

Antes del Motor de Descubrimiento Avanzado quedan: medir calibración cronológica
por mercado, conciliar reglas de liquidación y equivalencias de competición con
evidencia específica, introducir asociaciones nuevas con pruebas múltiples y
construir expedientes cualitativos. El selector aún no importa automáticamente
bindings de identidad: conserva pendientes visibles. No se declara completo ese
adaptador ni el orquestador LLM. No hay picks, EV ni NO BET generados.
