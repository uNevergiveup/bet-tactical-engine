# Comandos de Prueba Manual y Flujo Operativo

> Nota: no ejecutes barridos reales ni picks sin confirmación explícita; los comandos con acceso externo exigen `--confirm-external`.
>
> **ADVERTENCIA IMPORTANTE:** Los identificadores de evento `7990001` y `7990002` mostrados en los ejemplos de este documento son **URLs ficticias de ejemplo exclusivamente para documentación y pruebas sintéticas**. **NO deben utilizarse en pruebas reales**; en ejecuciones reales, sustitúyelos siempre por la URL exacta copiada de `bwin.es`.

## 1. Barrido integral PREMATCH con el nuevo alcance optativo (`top-tier-and-senior-cups`)
```bash
PYTHONPATH=. .venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --window ampliada \
  --competition-scope top-tier-and-senior-cups \
  --online \
  --confirm-external
```
Para conservar exactamente el filtro legado (solo primeras divisiones nacionales):
```bash
PYTHONPATH=. .venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --competition-scope domestic-top-tier \
  --online \
  --confirm-external
```

## 2. Vinculación manual de una URL Bwin (registro offline -> `BWIN_MANUAL_LINK_PENDING_VALIDATION`)
```bash
PYTHONPATH=. .venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --bind-bwin-url \
  --manual-bwin-url 1628569=https://www.bwin.es/es/sports/eventos/ranchers-bees-doma-united-2:7990001
```

## 3. Vinculación manual de múltiples URLs (repetible o vía archivo JSON/CSV)
Vía argumentos repetibles:
```bash
PYTHONPATH=. .venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --bind-bwin-url \
  --manual-bwin-url 1628569=https://www.bwin.es/es/sports/eventos/ranchers-bees-doma-united-2:7990001 \
  --manual-bwin-url 1645880=https://www.bwin.es/es/sports/eventos/cotonsport-stade-renard-2:7990002
```
Vía archivo JSON (`[{"fixture_id": 1628569, "bwin_url": "..."}, ...]`):
```bash
PYTHONPATH=. .venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --bind-bwin-url \
  --manual-bwin-file ruta/manual_bwin.json
```

## 4. Revalidación en vivo + captura de cuotas (sin saltar controles PREMATCH)
```bash
PYTHONPATH=. .venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --bind-bwin-url \
  --manual-bwin-url 1628569=https://www.bwin.es/es/sports/eventos/ranchers-bees-doma-united-2:7990001 \
  --verify-manual-bwin \
  --scrape-manual-bwin \
  --confirm-external
```

## 5. Reintento de scraping de cuotas sin alterar registros congelados
```bash
PYTHONPATH=. .venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --bind-bwin-url \
  --manual-bwin-url 1628569=https://www.bwin.es/es/sports/eventos/ranchers-bees-doma-united-2:7990001 \
  --verify-manual-bwin \
  --scrape-manual-bwin \
  --refresh-existing-odds \
  --confirm-external
```

## 6. Comportamiento ante duplicados y discrepancias
- **Duplicado idéntico** (mismo `fixture_id`, mismo `bwin_event_id` y misma URL): idempotente; conserva verificación previa si ya estaba validada.
- **Duplicado contradictorio** (mismo `fixture_id` con distinto `bwin_event_id` o mismo `bwin_event_id` en dos `fixture_id`s): aborta con código de salida `2` (`MANUAL_BWIN_DUPLICATE_FIXTURE_CONFLICT` / `MANUAL_BWIN_DUPLICATE_EVENT_ACROSS_FIXTURES`).
- **Discrepancia de equipos/hora/categoría:** marca `identity_status = BWIN_IDENTITY_CONFLICT`, bloquea el scraping y retorna código `2`.

## 7. Revisión de artefactos generados
- Cola de reconciliación Bwin: `prospective_trial/bwin/bwin_reconciliation_queue.json`
- Vinculaciones manuales: `prospective_trial/bwin/manual_bwin_links.json`
- Denominadores del embudo: `prospective_trial/pipeline_funnel_denominators.json`
- Tabla diaria CSV: `prospective_trial/partidos_diarios.csv`
