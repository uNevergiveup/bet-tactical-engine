# RUNBOOK OPERATIVO — PRÓXIMO PARTIDO PREMATCH

## 1. Ficha Verificada del Siguiente Candidato (#1 Ranking Táctico)

- **Partido:** `Remo` vs `Chapecoense-sc`
- **Competición:** `Brasileiro Serie A` (`league_id=71`, `season=2026`, `country=Brazil`, categoría `M / SENIOR`)
- **API-Football Fixture ID:** `1492456` (`home_id=1198`, `away_id=132`)
- **Bwin Event ID:** `7901472`
- **Bwin URL:** `https://www.bwin.es/es/sports/eventos/clube-do-remo-chapecoense-2:7901472`
- **Kickoff (`Europe/Madrid`):** `2026-11-19T00:00:00+01:00` (`2026-11-18T23:00:00+00:00`)
- **Nivel y Cobertura:**
  - `selection_tier`: `A` (`research_eligible=True`, `eligible=True`)
  - `Tactical Research Score`: `71.15` | `Market Execution Readiness`: `100` | `Orden conjunto`: `82.69`
  - Cobertura histórica: `17` partidos disponibles, `8` detallados, `32` series utilizables (`cards`, `corners`, `fouls`, `goals`, `offsides`, `shots`), `7` señales descriptivas (`corners`, `fouls`, `shots`).
  - Mercados Bwin verificados en preflight: `14` hipótesis directas, `56` indirectas, `541` mercados de jugador/XI.

---

## 2. Ventanas Temporales Obligatorias (`Europe/Madrid`)

| Operación | Límite Relativo | Hora Límite Exacta (`Europe/Madrid`) |
|---|---|---|
| Selección y `prepare` completo | `>= T-45m` | **Hasta `2026-11-18T23:15:00+01:00`** |
| Adquisición / confirmación de 22 titulares (`acquire-lineups` / manual) | `T-60m .. T-15m` | **Corte objetivo `2026-11-18T23:45:00+01:00`** |
| Cierre coordinado `close` (`precheck -> refresh-odds -> finish`) | `>= T-5m` | **Hasta `2026-11-18T23:55:00+01:00`** |
| Cierre absoluto PREMATCH (`kickoff`) | `T-0m` | **`2026-11-19T00:00:00+01:00`** |

---

## 3. Secuencia Exacta de Comandos CLI (Validados contra `--help`)

### Paso 1 — Selección explícita con refresco de discovery y cuotas de preflight (la captura de las `16:57` supera los 120 min)

```bash
.venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --rank \
  --sync-bwin \
  --select-verified \
  --fixture-id 1492456 \
  --preselection outputs/discovery/2026-11-18_1500_0300/evaluations/20261118T163044121035Z_81e8029defcf/prospective_trial/enriched/preselection.json \
  --refresh-existing-odds \
  --confirm-external
```

*(Nota: la salida imprimirá la ruta exacta `<SELECTED_MATCH_PATH>` dentro de `outputs/phase3/2026-11-18_1500_0300/runs/<RUN_ID>/fixtures/1492456/selection_pipeline/selected_match.json`).*

### Paso 2 — Verificar selección y preparar expediente único (`>= T-45m`, antes de las `23:15+01:00`)

```bash
.venv/bin/python prematch_single.py \
  --selected <SELECTED_MATCH_PATH> \
  check --reuse-odds

.venv/bin/python prematch_single.py \
  --selected <SELECTED_MATCH_PATH> \
  --confirm-external \
  prepare --reuse-odds
```

### Paso 3 — Investigación cualitativa (Perplexity + Grok; Gemini opcional)

Guardar el informe consolidado en la ubicación canónica del expediente:
- `outputs/matches/7901472/instructions/reporte_cualitativo_7901472.md` (o `instructions/reporte_cualitativo_7901472.md`).

### Paso 4 — Adquisición y confirmación de 22 titulares (`~T-55m .. T-15m`)

```bash
.venv/bin/python prematch_single.py \
  --selected <SELECTED_MATCH_PATH> \
  --confirm-external \
  acquire-lineups
```

Si API-Football aún no publica los 22 titulares y se deposita el JSON manual verificado en `outputs/matches/7901472/lineups/manual/manual_lineups_7901472.json`:

```bash
.venv/bin/python prematch_case.py \
  sync-manual-lineups \
  --event-id 7901472
```

### Paso 5 — Cierre coordinado único (`close`: Precheck + Refresh de cuotas + Finish + Shadow, `>= T-5m`, antes de las `23:55+01:00`)

```bash
.venv/bin/python prematch_single.py \
  --selected <SELECTED_MATCH_PATH> \
  --confirm-external \
  close \
  --shadow-mode
```

*(El subcomando `close` verifica contexto, identidad, 22 titulares e informe antes de tocar el scraper; refresca `cuotas_bwin_7901472.txt`; reevalúa Shadow; ejecuta `finish` inmediatamente; verifica que `ranking_probabilistico_7901472.json` utilice el `SHA-256` recién capturado y emite `outputs/matches/7901472/CODEX_7901472.md`).*
