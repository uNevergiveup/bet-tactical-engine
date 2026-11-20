# Ranking Antes vs. Después (Replay Offline sobre 18/11/2026)

Replay offline en solo lectura sobre `outputs/discovery/2026-11-18_1500_0300/evaluations/20261118T134610142084Z_c515d959b536/prospective_trial/enriched/preselection.json` y los 5 preflights/scanner capturados en ese barrido (sin ejecutar red ni modificar expedientes).

## 1. Ranking Estadístico Independiente de Bwin (`statistical_ranking`)

| Posición Antes | Posición Después | Fixture ID | Partido | Score Antes | Score Después | Muestra (avail/det) | Efecto observado |
|---:|---:|---:|---|---:|---:|---|---|
| 1 | 2 | `1492451` | Coritiba vs Santos | 79.40 | 71.81 | 15/8-9 | Mantiene top-2; deja de perder `corners`/`shots` por `eff% < 50%` |
| 2 | 1 | `1492454` | Gremio vs Bahia | 78.44 | 72.56 | 14/8 | Sube a #1 por estabilidad y concordancia en 4 familias |
| 3 | **6** | `1650229` | Independiente Chorrera vs Tauro | 77.84 | 69.58 | 19/19 (solo `goals`) | Compite como `MINIMUM_EVIDENCE_MET` sin ventaja artificial sobre Brasil Serie A |
| 4 | 3 | `1549554` | Alianza Atletico vs FBC Melgar | 76.76 | 71.25 | 16/8-9 | 9 señales robustas en 6 familias recuperadas al corregir umbral efectivo |
| 5 | 8 | `1492450` | Corinthians vs Botafogo | 75.93 | 69.00 | 14/8-9 | Compite por robustez de señal |
| 6 | **4** | `1492453` | Flamengo vs Atletico Paranaense | 73.58 | 70.82 | 15/8 | Permanece en top-4 estadístico aunque Bwin fuera diferido por presupuesto |
| 7 | **7** | `1492452` | Cruzeiro vs Palmeiras | 73.22 | 69.38 | 14/8 | Permanece visible en cola de reconciliación Bwin |
| 8 | **5** | `1492456` | Remo vs Chapecoense-sc | 71.33 | 70.51 | 17/8 | Sube por consistencia de `corners`/`fouls`/`shots` |
| 9 | 9 | `1628569` | Ranchers Bees vs Doma United | 68.75 | 59.34 | 12/12 | 0 señales soportadas; queda en `BWIN_NOT_FOUND_IN_SCANNED_PAGES`, nunca `SIN_CUOTAS` |
| 10 | 11 | `1645273` | Inter Moengotapoe vs FC Inter Wanica | 64.39 | 43.09 | 5/5 | Muestra corta (`n=5`) contraída explícitamente por `sample_precision` |
| 11 | 10 | `1645880` | Cotonsport vs Stade Renard | 62.90 | 57.14 | 4/4 | Muestra corta (`n=4`) deja de recibir `100%` por `coverage`; contraída por incertidumbre |

## 2. Ranking Conjunto con Snapshot Comercial (`ranking`, 5 verificados + 6 pendientes)

- **Antes (18/11/2026):** `1650229` (Panamá, solo `goals`) era #1 (`83.50`) y `SELECCIONADO` por encima de `1492451` (`81.64`) y `1492454` (`81.06`), debido a que `1492451`/`1492454` recibían penalización por contradicciones en familias secundarias que Panamá ni siquiera medía.
- **Después:**
  1. `1492454` Gremio vs Bahia (`tier=A`, `research_priority=83.54`, `tact=72.56`)
  2. `1492451` Coritiba vs Santos (`tier=A`, `research_priority=83.09`, `tact=71.81`)
  3. `1492450` Corinthians vs Botafogo (`tier=A`, `research_priority=81.40`, `tact=69.00`)
  4. `1549554` Alianza Atletico vs FBC Melgar (`tier=A`, `research_priority=79.55`, `tact=71.25`)
  5. `1650229` Independiente de La Chorrera vs Tauro FC (`tier=A`, `research_priority=78.55`, `tact=69.58`)
- Los partidos diferidos por presupuesto (`1492452`, `1492453`, `1492456`) y los no encontrados en páginas escaneadas (`1628569`, `1645880`, `1645273`) permanecen en `statistical_ranking` y en `bwin_reconciliation_queue.json` con `offer_proven_absent = False`.
