# Modelos Experimentales M0–M4 y Ablaciones OOS — Iteración 2.1B

**Proyecto:** `bet-tactical-engine`  
**Módulo aislado:** [experimental_v2_1b.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py)  
**Runner reproducible:** [run_iteracion_2_1b_experiments.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/run_iteracion_2_1b_experiments.py)  
**Comando:** `PYTHONPATH=. .venv/bin/python tools/run_iteracion_2_1b_experiments.py --out-dir audit/iteracion_2_1b` (`seed = 42`, `bootstrap = 2,000`).

---

## 1. Esquema de validación temporal estricta

- **`train_fit` (`n = 1,083`, `2025-10-11` .. `2026-09-05`, `27` selecciones):** estimación de tasas base por estrato (`CLUB`: `(1.5938, 1.3390)`, `NATIONAL_TEAM`: `(1.8148, 1.4444)`).
- **`val_select` (`n = 1,087`, `2026-09-05` .. `2026-09-26T13:00Z`):** selección única de hiperparámetros (`M2`: `sos_alpha=0.4, elo_weight=0.0`; `M3`: `half_life_days=None, venue_blend=0.0`; `M4`: `shrink_k=12.0, rho=-0.04`).
- **`test_strict` (`n = 2,319`, `2026-09-26T14:00Z` .. `2026-11-14`, `2,272 CLUB` + `47 NATIONAL_TEAM`):** evaluación única final con hiperparámetros congelados.

---

## 2. Resultados OOS comparados (`test_strict`, `n = 2,319`)

| Variante | 1X2 RPS | 1X2 Log-Loss | 1X2 Brier | HomeWin ECE | Over 2.5 Brier | BTTS Brier | Sesgo Fav `>=0.55` | Sesgo Goles Home / Away | `ms/partido` | `Δ LL` vs M0 (IC 95% día) | `P(mejora)` día / sem |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **M0** (Baseline L20) | `0.21823` | `1.02074` | `0.20401` | `0.0557` | **`0.23562`** | `0.24387` | `-0.1434` | `-0.1696 / +0.0071` | `0.099` | `—` | `—` |
| **M1** (Multiplicativo) | `0.21682` | `1.01670` | `0.20281` | `0.0356` | `0.24277` | `0.24879` | `+0.0377` | `-0.0296 / -0.0759` | `0.054` | `-0.00404` (`[-0.01376, +0.00500]`) | `0.822 / 0.828` |
| **M2** (`M1 + SoS 0.4`) | `0.21641` | `1.01561` | `0.20254` | **`0.0317`** | `0.24238` | `0.24830` | `+0.0338` | `-0.0322 / -0.0774` | `0.229` | `-0.00513` (`[-0.01471, +0.00353]`) | `0.882 / 0.902` |
| **M3** (`M2 + venue_blend=0`) | **`0.21599`** | **`1.01382`** | **`0.20226`** | `0.0408` | `0.23922` | `0.24478` | **`+0.0045`** | `-0.1091 / -0.0163` | `0.226` | **`-0.00692`** (`[-0.01428, -0.00007]`) | `0.976 / 0.979` |
| **M4** (`M3 + shrink_k=12`) | `0.21615` | `1.01442` | `0.20243` | `0.0410` | `0.23728` | `0.24386` | `-0.0419` | `-0.1101 / -0.0194` | `0.226` | `-0.00632` (`[-0.01129, -0.00160]`) | **`0.994 / 0.998`** |
| **ABL_only_elo_additive** (`Elo 0.25`) | `0.21648` | `1.01552` | `0.20270` | `0.0525` | `0.23576` | **`0.24379`** | `-0.0597` | `-0.1623 / +0.0074` | **`0.056`** | `-0.00522` (`[-0.00814, -0.00256]`) | **`0.9995 / 1.000`** |
| **ABL_only_sos_additive** | `0.21848` | `1.02158` | `0.20417` | `0.0544` | `0.23644` | `0.24400` | `-0.0875` | `-0.1696 / +0.0015` | `0.229` | `+0.00084` (`[-0.00191, +0.00320]`) | `0.294` |
| **ABL_only_recency_additive** | `0.21881` | `1.02235` | `0.20438` | `0.0536` | `0.23624` | `0.24387` | `-0.1159` | `-0.1689 / -0.0001` | `0.069` | `+0.00161` (`[-0.00096, +0.00378]`) | `0.116` |
| **ABL_only_shrink_additive** | `0.21916` | `1.02355` | `0.20466` | `0.0495` | `0.23647` | `0.24394` | `-0.1316` | `-0.1642 / -0.0166` | `0.054` | `+0.00281` (`[-0.00019, +0.00527]`) | `0.031` |

---

## 3. Análisis por componente y estrato (`CLUB` vs `NATIONAL_TEAM`)

1. **Interacción multiplicativa (`M1`) vs `M3`/`M4`:** `M1` elimina la compresión de favoritos (`-14.34 pp -> +3.77 pp`), pero con `venue_blend = 0.25` y `shrink_k = 8` sobre-extiende extremos y deteriora `Over 2.5` (`0.24277`) y `BTTS` (`0.24879`). Eliminar `venue_blend` redundante (`M3`) y aumentar contracción a `shrink_k = 12.0` (`M4`) recupera `BTTS` (`0.24386`) y acerca `Over 2.5` (`0.23728`), manteniendo `1X2 Log-Loss = 1.01442` (`IC 95%` estrictamente negativo) y `fav_ge_0_55_bias = -4.19 pp`.
2. **Fuerza del rival (`SoS` vs `Elo` as-of):**
   - `SoS` por tasas previas del rival cubre el `60.29%` de apariciones en `test_strict`; aporta mejora marginal sobre `M1` (`RPS 0.21682 -> 0.21641`), pero por sí solo sobre `M0` (`ABL_only_sos_additive`) no mejora (`prob = 0.294`).
   - **`AsOfEloTracker` aislado por estrato** (`ABL_only_elo_additive`) alcanza **`100%` de cobertura (`>= 3` partidos)** en `test_strict` y produce la mejora más limpia y libre de regresiones: `1X2 RPS -0.00175`, `Log-Loss -0.00522` (`prob = 1.000` semanal), sin dañar `Over 2.5` (`0.23576`) ni `BTTS` (`0.24379`).
3. **Diferencias `CLUB` (`n = 2,272`) vs `NATIONAL_TEAM` (`n = 47`):**
   - En `CLUB`, `M4` mejora `RPS` de `0.21852` (`M0`) a `0.21662` y reduce `fav_ge_0_55` de `-13.01 pp` a `-3.71 pp`; `ABL_only_elo_additive` logra `0.21692` (`-5.29 pp`).
   - En `NATIONAL_TEAM` (`n = 47`, muestra pequeña), `M2` (`0.18626`), `M3` (`0.18696`), `M4` (`0.19330`) y `ABL_only_elo_additive` (`0.19500`) mejoran a `M0` (`0.20447`), aunque `shrink_k=12` penaliza algo frente a `M2/M3` por el menor tamaño muestral histórico de selecciones.

---

## 4. Resultados en Argentina–Costa Rica (`7909713`, snapshot congelado `L6`)

En [SENSIBILIDAD_7909713_MODELOS.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_1b/SENSIBILIDAD_7909713_MODELOS.json) (exclusivamente sobre los `6/6` partidos del snapshot histórico, sin usar cuotas Bwin `1.055` ni L20 externos):

| Modelo | `lambda_home` | `lambda_away` | `P(1)` / `P(X)` / `P(2)` | Banda `P(1)` | `Δ P(1)` vs M0 | Observación |
|---|---|---|---|---|---|---|
| **M0** | `2.1916` | `1.0162` | `0.6372 / 0.2022 / 0.1606` | `[0.4030, 0.8271]` | `0.0000` | Prior absorbe `63.75%` (`n_eff=4.55`) |
| **M1** | `3.2551` | `0.7070` | `0.8547 / 0.0990 / 0.0463` | `[0.6834, 0.9494]` | `+0.2175` | Interacción multiplicativa con `venue_blend=0.25` |
| **M2** | `3.2340` | `0.7070` | `0.8527 / 0.1003 / 0.0471` | `[0.6802, 0.9485]` | `+0.2154` | Cobertura SoS parcial (`20%`), sin Elo previo en corpus |
| **M3** | `2.8257` | `0.9390` | `0.7609 / 0.1446 / 0.0945` | `[0.5485, 0.9026]` | `+0.1236` | Al quitar `venue_blend` cae exageración de `M1` |
| **M4** | `2.5645` | `1.0521` | `0.6970 / 0.1715 / 0.1315` | `[0.4701, 0.8653]` | `+0.0598` | `shrink_k=12` (`72%` peso prior sobre `n=6`) |
