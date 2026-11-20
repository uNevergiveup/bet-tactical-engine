# Diseño y Reglas Aplicadas — Corrección Controlada

## 1. Clasificación de competiciones y nuevo alcance optativo (`filter_major_leagues.py`, `prematch_daily.py`)
- **Taxonomía estructural (`competition_category`):** `YOUTH`, `RESERVES`, `SENIOR_MALE_TOP_TIER`, `SENIOR_FEMALE_TOP_TIER`, `SENIOR_ELIGIBLE_CUP_OR_INTERNATIONAL`, `LOWER_DIVISION`, `CLUB_FRIENDLY`, `UNKNOWN_CATEGORY`.
- **Reglas estrictas:**
  - `YOUTH_PATTERN` y `RESERVE_LEAGUE_PATTERN` / `RESERVE_TEAM_PATTERN` se evalúan antes que las políticas de catálogo para excluir juveniles (`U17`-`U23`, `Youth`, `Primavera`) y filiales (`Reserves`, `II`, `III`, `B`) con evidencia verificable.
  - `W`, `Women` y `Cup` nunca se interpretan como juvenil.
  - En copas adultas (`NATIONAL_CUP`, `NATIONAL_OFFICIAL`), se verifica también que ninguno de los participantes sea juvenil o filial (`participant_category_check`).
  - Los encuentros de categoría desconocida permanecen en `review` (`PENDING_CLASSIFICATION`), nunca promovidos automáticamente.
- **Políticas soportadas (`--policy` / `--competition-scope`):**
  1. `adult-open` (default existente, invariante para `test_discovery_policy_blocks` y `test_bwin_discovery_v31`).
  2. `domestic-top-tier` (compatibilidad histórica conservada, pero sustituyendo `YOUTH_RESERVES_OR_CUP` por motivos diferenciados: `YOUTH_*`, `RESERVE_*`, `LOWER_DIVISION`, `CUP_OR_INTERNATIONAL_EXCLUDED_BY_DOMESTIC_TOP_TIER`).
  3. `top-tier-and-senior-cups` (nuevo alcance optativo): admite primeras divisiones adultas M/F (`national_tier == 1` o `CATALOG_BY_COUNTRY`), competiciones oficiales absolutas (p. ej. `UEFA Champions League Women`, ID `525`) y copas nacionales adultas reconocidas (p. ej. `Copa del Rey`, ID `143`), excluyendo juveniles, reservas y divisiones inferiores verificadas (`national_tier >= 2` / `KNOWN_EXCLUDED_IDS`).

## 2. Cobertura mínima por familia de mercado (`preselect_matches.py`, `rank_matches.py`, `prematch_runtime.py`)
- `assess_family_evidence()` evalúa independientemente cada familia (`goals`, `corners`, `shots`, `cards`, `fouls`, `offsides`) y clasifica cada partido en:
  - `NO_STATISTICAL_DATA`
  - `BELOW_MINIMUM_EVIDENCE`
  - `MINIMUM_EVIDENCE_MET` (al menos 1 familia cumple `min_series_n >= 4` y `completed_total >= 4`)
  - `RICH_EVIDENCE_AVAILABLE` (>= 2 familias detalladas con `n >= 6`, o `goals >= 10` y >= 1 familia detallada `n >= 6`)
- `MINIMUM_EVIDENCE_MET` y `RICH_EVIDENCE_AVAILABLE` compiten en el ranking (`rank_matches.py` y `prematch_runtime.py`). Un partido que cumple `goals` no es excluido por carecer de córners o tarjetas.
- Se preservan intactos los umbrales de producción y EV (`motor_probabilistico.py`, `bet_engine/modeling/*`, `prematch_opportunity_scanner.py`).

## 3. Ranking basado en robustez de señales (`prematch_tactical_signal_engine.py`, `prematch_daily.py`)
- Se elimina `coverage` (20%) del vector de pesos de puntuación y se anula la bonificación automática por capacidades/familias (`declared_coverage_bonus_applied = 0.0`).
- Nuevo vector `WEIGHTS` (`2.1-robust-tactical-signals`):
  - `sample_precision` (0.20): `min(n/10, 1.0) * (n / (n + 6))` — contracción empírica Bayesiana explícita para evitar que muestras pequeñas (`n=4`) compitan en igualdad de condiciones con muestras sólidas (`n>=12`).
  - `stability` (0.25): `1/(1+cv)` modulada por consistencia temporal reciente (`L5` vs `L5` previo).
  - `agreement` (0.20): concordancia entre producción propia y concesión rival.
  - `sensitivity` (0.20): combinación `0.7 * LOO + 0.3 * window_resilience` (resistencia a perturbación de un partido y de ventana reciente vs completa).
  - `venue` (0.10): consistencia y tamaño muestral en sede local/visitante.
  - `profile` (0.05): perfil táctico verificado pre-cutoff.
- Se adjunta `uncertainty` en cada señal con `laplace_shrunk_ceiling_rate = (n+1)/(n+2)` (p. ej. `10/10 -> 0.9167`, nunca `100%`), `uncertainty_se_scale = 1/sqrt(n)` y `sos_adjustment_status`.
- El filtro `usable` usa `historical_effective_coverage_pct >= 40` y `requested_data_coverage_pct >= 50` para no castigar `detail_limit=8` frente a `L20`.
- En `selection_rows()`, las contradicciones se evalúan sobre las señales principales consideradas (`top_signals[:3]`) en lugar de penalizar a un partido por tener más familias medidas.

## 4. Separación Bwin, presupuestos y URLs manuales (`bwin_manual_links.py`, `prematch_runtime.py`, `prematch_daily_csv.py`)
- El `statistical_ranking` es 100% independiente de Bwin (`commercial_readiness_not_used = True`).
- Estados canónicos de búsqueda Bwin (`bwin_search_status`): `BWIN_VERIFIED`, `BWIN_NOT_SCANNED_BUDGET`, `BWIN_SEARCH_INCOMPLETE`, `BWIN_NOT_FOUND_IN_SCANNED_PAGES`, `BWIN_IDENTITY_CONFLICT`, `BWIN_MANUAL_LINK_PENDING_VALIDATION`, `BWIN_MANUAL_LINK_VERIFIED`.
- Se persisten `prospective_trial/bwin/bwin_reconciliation_queue.json`, `prospective_trial/bwin/manual_bwin_links.json` y `prospective_trial/pipeline_funnel_denominators.json`.
- Los candidatos diferidos por presupuesto mantienen `estado_final = NO_EVALUADO_POR_LIMITE` y `budget_status = PENDING_BUDGET`, sin degradarse a `NO_DATA`, `NOT_OFFERED` ni `SIN_CUOTAS`.

## 5. Comportamientos intactos
- Sin cambios en `motor_probabilistico.py`, `bet_engine/modeling/*`, `bet_engine/orchestration/*` (modelos congelados M0/M0+Elo/M4/M4+Elo, Shadow, EV y registro prospectivo 2.2C.1 intactos), ni en las reglas de corte temporal P0 ni en `validate_prematch.py`.
