# BET ENGINE V4 — FASE 2B.3A
# Diseño Experimental A/B de Calibración y Especificación Estadística

**Fecha de generación:** `2026-11-19T01:38:37Z`  
**Rama:** `refactor/etapa2-fundamentos`  
**Script aislado de ejecución:** [`tools/run_fase2b3a_experiments.py`](file:///Users/antonio/Projects/bet-tactical-engine/tools/run_fase2b3a_experiments.py)  
**Salida estructurada:** [`audit/fase2b_experimentos/resultados_ab.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/resultados_ab.json)  
**Baseline de referencia congelado:** `CoherentMatchModeler_baseline_2.0_2.1A`  
**Manifiesto V2 de referencia (intacto):** [`MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json) (`sha256: 9e7eb6806a85361e7ed2a36d6904289751ed6dd32431717190a199d4a7298606`)

---

## 1. Objetivo y Reglas de Aislamiento

Este diseño experimental aísla y mide qué componentes estructurales del estimador Poisson/Dixon-Coles (`rho = -0.04`, `max_goals = 12`, `DEFAULT_PRIORS = (1.56, 1.26)`, `shrink_k = 8.0`, mezcla de sede `0.25`) provocan la compresión probabilística y la infravaloración observada de victoria local y favoritos en `CoherentMatchModeler_baseline_2.0_2.1A`.

**Restricciones metodológicas estrictas:**
1. **No modificación de producción:** ni [`bet_engine/modeling/distributions.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py), ni los umbrales Shadow/BET, ni los expedientes PREMATCH cerrados en `outputs/matches/` fueron alterados.
2. **Separación estricta Train vs. Test vs. Holdout prospectivo:**
   - Toda calibración empírica de priors (`A1`, `A2`) y la clasificación principal de variantes ocurren exclusivamente mediante validación cronológica **walk-forward interna dentro de `train_strict`** (`< 2026-09-26T14:00:00+00:00`, `n = 2,173`).
   - `test_strict` (`>= 2026-09-26T14:00:00+00:00`, `n = 2,323`) se ejecuta una sola vez como **pasada confirmatoria bloqueada** sin selección ni ajuste de hiperparámetros sobre test.
   - No se evalúan datos prospectivos futuros (`> 2026-11-14T19:45:00+00:00`), ni cuotas Bwin, ni `ROI`, `EV`, `CLV` o el evento `7901472`.
   - No se emite acreditación `CALIBRATED_OUT_OF_SAMPLE`.

---

## 2. Verificación Previa de Referencia y Corpus Limpio (2B.2A)

Antes de comparar variantes, [`run_fase2b3a_experiments.py`](file:///Users/antonio/Projects/bet-tactical-engine/tools/run_fase2b3a_experiments.py) verificó el corpus certificado de Fase 2B.2A (`order_invariant=True`, `use_league_catalog=True`, catálogo de `263` ligas):

| Comprobación previa | Valor verificado |
|---|---:|
| Corpus PREMATCH elegible total | `4,496` |
| Partición `train_strict` (`< 2026-09-26T14:00:00+00:00`) | `2,173` |
| Partición `test_strict` (`>= 2026-09-26T14:00:00+00:00`) | `2,323` |
| Referencia secundaria legacy (sin catálogo/orden fijo) | `2,319` test (`4,489` total) |
| Paridad exacta `B0_baseline_frozen` vs `_estimate_team_rate_detailed` (`HOME` y `AWAY`, 200 fixtures) | `max_diff = 0.0` (`< 1e-12`) |
| RPS baseline `B0` en `test_strict` con catálogo limpio | `0.21829` |

---

## 3. Protocolo Walk-Forward Interno, Bloqueo Temporal y Cero Fugas

Para evitar cualquier fuga temporal o solapamiento de fixtures en variantes que ajustan priors empíricos (`A1`, `A2`):

### 3.1 Esquema Walk-Forward Principal (`train_wf_main`, `n = 1,447`)
Tres folds cronológicos expansivos contenidos íntegramente antes de `2026-09-26T14:00:00+00:00`, con `assert` explícito de intersección vacía (`fit_fids & val_fids == set()`) y filtro estricto `kickoff_utc < T_fold` y `available_at_utc <= T_fold`:

| Fold | Ventana de validación UTC | `val_n` | `fit_n` as-of (`< T_fold`) |
|---|---|---:|---:|
| `fold1` | `2026-08-26T00:30:00Z` a `< 2026-09-05T22:00:00Z` | `500` | `11,281` |
| `fold2` | `2026-09-05T22:00:00Z` a `< 2026-09-15T22:30:00Z` | `499` | `13,138` |
| `fold3` | `2026-09-15T22:30:00Z` a `< 2026-09-26T14:00:00Z` | `448` | `14,850` |
| **Total pooled WF** | **`2026-08-26T00:30:00Z` a `< 2026-09-26T14:00:00Z`** | **`1,447`** | — |

### 3.2 Partición Alternativa Desplazada (`train_wf_alt_partition`, `n = 1,550`)
Para comprobar estabilidad respecto a las fronteras de corte dentro de `train_strict`, se ejecuta un segundo esquema walk-forward con tres folds desplazados (`2026-08-22T14:15:00Z`, `2026-09-05T13:30:00Z`, `2026-09-13T01:10:00Z` hasta `< 2026-09-26T14:00:00Z`), evaluando `1,550` partidos distribuidos en `36` bloques diarios.

### 3.3 Pasada Confirmatoria Bloqueada (`confirmatory_test_strict_locked`, `n = 2,323`)
Ejecutada una sola vez sobre `test_strict`. En variantes empíricas (`A1`, `A2`), los priors usados proceden de un ajuste congelado estrictamente anterior al inicio del test (`< 2026-09-26T14:00:00Z`, `fit_n = 16,227`, media global `(1.6187, 1.3488)`, `DOMESTIC_LEAGUE = (1.5926, 1.2890)`, `UNKNOWN = (1.6590, 1.4880)`, `DOMESTIC_CUP = (1.8275, 1.8063)`).

---

## 4. Variantes Aisladas Evaluadas (15 variantes frente a `B0`)

Todas las variantes mantienen fijo el resto del pipeline (`rho = -0.04`, `max_goals = 12`, pesos recencia L6/L20 `0.35 / 0.65`, pesos ataque/defensa `0.55 / 0.45`, deduplicación H2H `0.50`, política rojas `exclude_any`, pesos de competición `1.0 / 0.50 / 0.25` salvo en `C4`).

### 4.1 Experimento A — Priors y Contracción Bayesiana (6 variantes)
| ID | Cambio aislado respecto a `B0` |
|---|---|
| `A1_emp_stratum_priors` | Priors empíricos por estrato ajustados as-of `< T_fold` con suavizado hacia global (`m_st = 25`), manteniendo `shrink_k = 8.0`. |
| `A2_emp_league_priors` | Priors jerárquicos empíricos por `league_id` as-of `< T_fold` contraídos hacia el estrato dominante (`m_lg = 30`), manteniendo `shrink_k = 8.0`. |
| `A3_shrink_k_4` | Priors fijos `(1.56, 1.26)` con menor contracción (`shrink_k = 4.0`). |
| `A4_shrink_k_2` | Priors fijos `(1.56, 1.26)` con contracción mínima (`shrink_k = 2.0`). |
| `A5_shrink_k_12` | Priors fijos `(1.56, 1.26)` con mayor contracción (`shrink_k = 12.0`). |
| `A6_kish_neff_k6` | Tamaño muestral efectivo de Kish `n_eff = (sum w)^2 / sum(w^2)` en lugar de `sum(w)` como denominador de contracción, con `shrink_k = 6.0`. |

### 4.2 Experimento B — Localía y Contracción por Sede (5 variantes)
En `B0`, el promedio de sede no tiene contracción previa (`venue_n >= 3`) y recibe solo peso `0.25` frente al `0.75` de la tasa combinada `HOME + AWAY`. En `B3..B5` se introduce un ancla bayesiana de sede (`anchor = max(0.15, base_shrunk * (prior_sede / prior_medio))`, con `venue_k = 4.0`) para controlar ruido muestral cuando `venue_n` es pequeño.

| ID | Cambio aislado respecto a `B0` |
|---|---|
| `B1_venue_blend_0` | Desactiva la mezcla empírica de sede (`venue_blend = 0.0`). |
| `B2_venue_blend_50_unshrunk` | Sube mezcla de sede a `0.50` sin contracción bayesiana (misma regla `venue_n >= 3`). |
| `B3_venue_shrunk_50_k4` | Mezcla `0.50` con promedio de sede contraído hacia el ancla (`venue_k = 4.0`). |
| `B4_venue_shrunk_70_k4` | Mezcla `0.70` con promedio de sede contraído (`venue_k = 4.0`). |
| `B5_venue_shrunk_80_k4` | Mezcla `0.80` con promedio de sede contraído (`venue_k = 4.0`). |

### 4.3 Experimento C — Política de Competiciones e Identidad (4 variantes)
| ID | Cambio aislado respecto a `B0` |
|---|---|
| `C1_exclude_friendlies_fb4` | Excluye amistosos de club (`league_id in {667, ...}` o nombre friendly/amistos) del historial si quedan `>= 4` partidos no amistosos; si quedan `< 4`, mantiene el historial completo. |
| `C2_exclude_friendlies_strict` | Excluye todo amistoso de club sin fallback; registra explícitamente la caída de cobertura por debajo de 6 antecedentes limpios (`strict_non_friendly_subset`). |
| `C3_restrict_cups_league_ge6` | Cuando el objetivo es `DOMESTIC_LEAGUE` y el equipo tiene `>= 6` partidos de liga, excluye partidos `DOMESTIC_CUP` si tras filtrarlos quedan `>= 6`. |
| `C4_strict_comp_identity` | Activa `strict_competition_identity=True` (impide `UNKNOWN == UNKNOWN` y exige `league_id > 0` y `season > 0` para peso `1.0`). |

---

## 5. Métricas, Inferencia Bootstrap y Criterios de Clasificación

- **Métricas primarias:** Ranked Probability Score (`RPS` 1X2), `LogLoss 1X2`, `Brier 1X2`.
- **Métricas secundarias y calibración:** `Brier O2.5`, `LogLoss O2.5`, `Brier BTTS`, `ECE (5 bins)` para victoria local (`hw`), visitante (`aw`) y `O2.5`, sesgos marginales `pred - obs` (`1`, `X`, `2`), sesgo de tasa `lambda_home - hg` y `lambda_away - ag`.
- **Subgrupos e invarianzas de sensibilidad:**
  - `favorites_p_ge_0_55` y diferenciales de fuerza `lambda_home - lambda_away > 0.50` / `< -0.50`;
  - `HIGH_VENUE_GAP` (`|mean(HOME) - mean(AWAY)| >= 0.70` con `>= 3` partidos por sede);
  - Estratos (`DOMESTIC_LEAGUE`, `UNKNOWN`, `DOMESTIC_CUP`, `NATIONAL_TEAM`);
  - Profundidad muestral (`LOW_DEPTH_MIN_6_9` vs `DEPTH_10_19`);
  - Cobertura histórica (`BOTH_SIDES` vs `RECONSTRUCTED` en `test_strict`);
  - Subconjuntos efectivamente modificados (`changed_subset_wf`, `changed_subset_test`) en cada variante;
  - Sensibilidad de ventana temporal `L6 / L10 / L20` sobre `B0, A1, A3, B3, B4, C1, C4`.
- **Control de dependencia temporal:** Bootstrap pareado por bloques (`B = 2,000`, `seed = 42`) agrupando por día (`n_blocks` diarios, `5` bloques semanales como verificación auxiliar en WF).

### Regla pre-registrada de veredicto (`_classify_verdict`, basada solo en `train_wf`)
- **`EVIDENCIA_INSUFICIENTE`**: si `n_affected < 50`.
- **`MEJORA_RESPALDADA`**: si en `train_wf_main` los IC 95% bootstrap diarios de `ΔRPS` y `ΔLogLoss_1X2` son estrictamente `< 0`, los **tres folds** de WF mejoran `RPS` (`delta < 0`), el IC 95% de `ΔRPS` en la partición alternativa es estrictamente `< 0` y no se detecta deterioro material en `Brier O2.5`.
- **`EMPEORA`**: si algún IC 95% de `ΔRPS` o `ΔLogLoss_1X2` excluye `0` positivamente o si empeoran simultáneamente `mean_delta RPS > 0`, `mean_delta LogLoss > 0` y `mean_delta RPS_alt > 0`.
- **`MEJORA_NO_CONCLUYENTE`**: resto de casos (efectos nulos, señales mixtas entre métricas/folds o intervalos que cruzan cero).
