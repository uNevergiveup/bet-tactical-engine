# Objetivo 7 — integración del flujo diario

## Resultado

`prematch_daily.py` continúa siendo el único flujo diario. No se ha creado otro
pipeline. La validación de esta entrega es completamente offline y no ejecuta
apuestas, API-Football, scraping Bwin ni análisis en vivo.

## Reproducción del fallo congelado (12/11/2026)

Fuente: `outputs/discovery/2026-11-12_1500_0300/prospective_trial`.

- 62 procesos registrados, 1.666,8204 s acumulados.
- 48 ejecuciones de `bwin_discovery.py`: 1.590,8141 s (95,44 % del tiempo
  registrado); 47 terminaron con código 0 y una con código 1.
- Dos preflights: Barcelona W–Arsenal W quedó fuera del margen T-45 y Benfica
  W–Austria Wien W fue verificado.
- El scanner leyó 266 selecciones; 245 superaban el filtro de precio.
- Distribución real: 128 mercados por periodo sin modelo, 80 mercados sin
  modelo específico, 14 combinados, 3 props de jugador, 21 precios bajo el
  mínimo configurado y 20 totales FT modelables únicamente como diagnóstico
  por muestra corta. Resultado correcto: cero candidatos/picks.
- El código 1 del scanner no describía una excepción: el JSON, CSV y Markdown
  se habían escrito correctamente. Se emitía porque uno de los dos preflights
  estaba bloqueado. Ahora esa situación es `COMPLETED_WITH_BLOCKED_FIXTURES`
  con exit 0; una excepción real conserva exit 2.

## Correcciones

1. La reconciliación 5.1 se consume por Fixture ID y Event ID. Se verifican
   IDs de equipos, liga, temporada, kickoff, competición, categoría, URL y el
   SHA del inventario fuente. El vínculo persistido sólo permite abrir la URL
   directa: identidad y hora se vuelven a inspeccionar antes del scraper.
2. Con vínculo persistido no se recorre el catálogo completo. El valor por
   defecto queda limitado a un descubrimiento completo para fixtures sin
   vínculo y a un intento de captura; los límites siguen siendo configurables.
3. El motor diario incorpora los expedientes 6.1 si fixture, IDs, hashes y
   cortes temporales son compatibles. Se publica `statistical_ranking`
   separado del ranking/preparación comercial. Las señales 6.1 mantienen
   `commercial_use=false` y no crean picks.
4. El scanner reconoce la etiqueta vigente `MUESTRA_DESCRIPTIVA_OK`; no
   promociona `MUESTRA_PARCIAL`. Una coincidencia sin dirección queda marcada
   como baseline descriptivo, no como valor.
5. El sidecar comercial ya no exige que el SHA del inventario canónico Bwin
   sea igual al SHA de la preselección enriquecida. Exige el enlace correcto
   cola–scanner y usa discovery como evidencia opcional que puede bloquear por
   conflicto.
6. El handoff diferencia `TECHNICAL_FAILURE` de
   `INSUFFICIENT_EVIDENCE`/`NO_BET_LEGITIMATE`. Un NO BET legítimo termina el
   comando diario con exit 0; un fallo técnico termina con exit 2.
7. Cada run escribe `phase_metrics.json` con tiempos de preparación reutilizada,
   ranking estadístico, identidad/captura, scanner, vistas finales y total.

## Validación offline

- Ocho identidades UEFA 5.1 recuperadas de forma exacta, incluidos Italy–France
  `7905471`, Czechia–Spain `7905479` y England–Croatia `7905480`.
- Ocho expedientes 6.1 aceptados al verificarlos contra su baseline y corte.
- En el replay de la preselección diaria congelada se reutilizan seis: Türkiye
  y Armenia ya no estaban en aquella preselección al iniciarse el scan por el
  margen operativo; no se reintroducen retrospectivamente.
- Replay del sidecar: 464 señales preservadas, 0 enlaces verificados y 0 errores
  de SHA falsos. Estados: 308 `NO_PRICE`, 124 `BWIN_NOT_FOUND`, 16
  `PREFLIGHT_INVALID` y 16 `MARKET_NOT_SUPPORTED`.
- Las 20 líneas FT del fixture verificado siguen bloqueadas por muestra corta.
  No se modificó ningún umbral ni regla comercial.

## Comando diario

```bash
.venv/bin/python -B prematch_daily.py \
  --online --confirm-external --window normal \
  --history 20 --details 8 --discovery-retries 1 \
  --bwin-identity-catalog outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json
```

Para una jornada futura puede indicarse el catálogo persistido vigente. Los
fixtures que no estén en él usan como máximo el descubrimiento completo
configurado. La revisión final de cuotas, T-45/T-5, frescura, XI y NO BET no se
relajan.
