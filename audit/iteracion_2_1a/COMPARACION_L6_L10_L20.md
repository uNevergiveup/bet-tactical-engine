# Comparación Estadística L6 vs L10 vs L20 e Incertidumbre — Iteración 2.1A (Fase A)

**Fuente:** [BASELINE_REPRODUCIBLE.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json) (`CoherentMatchModeler_baseline_2.0_2.1A`)  
**Split principal auditable (sin empates de frontera ni rojas objetivo):** `test_strict` (`n = 2,319` partidos: `2,272` `CLUB` + `47` `NATIONAL_TEAM`; `2026-09-26T14:00:00+00:00` .. `2026-11-14T19:45:00+00:00`).

---

## 1. Métricas comparadas sobre exactamente los mismos partidos (`n = 2,319`)

| Ventana | 1X2 Brier (por salida) | 1X2 Log-Loss | 1X2 RPS | HomeWin Brier / ECE | Over 2.5 Brier / ECE | BTTS Yes Brier / ECE | MAE Goles Total | Sesgo Home (`pred-obs`) |
|---|---|---|---|---|---|---|---|---|
| **L6** | `0.20683` | `1.03229` | `0.22232` | `0.240774` / `0.045112` | `0.237140` / `0.038392` | `0.243734` / `0.013718` | `1.4169` | `-0.1512` |
| **L10** | `0.20487` | `1.02429` | `0.21950` | `0.238005` / `0.049636` | **`0.235526`** / `0.035962` | **`0.243650`** / `0.014545` | `1.4075` | `-0.1519` |
| **L20** | **`0.20401`** | **`1.02074`** | **`0.21823`** | **`0.236720`** / `0.055714` | `0.235623` / `0.037950` | `0.243869` / `0.014677` | **`1.4070`** | `-0.1696` |

*(Los resultados en `split_2248_deterministic` siguen exactamente el mismo patrón: `1X2 RPS`: `0.22233 -> 0.21968 -> 0.21825`.)*

### Desglose por estrato (`test_strict`)
- **CLUB (`n = 2,272`):** `1X2 RPS` mejora de `0.22242` (`L6`) -> `0.21977` (`L10`) -> **`0.21852`** (`L20`), y `Log-Loss` de `1.03272` -> `1.02523` -> **`1.02173`**.
- **NATIONAL_TEAM (`n = 47`, muestra pequeña):** `1X2 RPS` mejora de `0.21755` (`L6`) -> `0.20660` (`L10`) -> **`0.20447`** (`L20`), `Log-Loss` de `1.01165` -> `0.97865` -> **`0.97258`**.

---

## 2. Validez estadística: Bootstrap por bloques temporales (`seed = 42`, `B = 2,000`)

Para controlar correlación diaria/semanal entre partidos de la misma fecha/jornada (`test_strict`, `n = 2,319`), se ejecutó bootstrap pareado por bloques de **día UTC (`47` bloques)** y por **semana ISO (`8` bloques)**:

| Contraste (`alt - ref`, negativo = mejora `alt`) | Métrica | Media delta | IC 95% (bloques diarios, 47) | IC 95% (bloques semanales, 8) | `P(alt mejora)` (día / semana) |
|---|---|---|---|---|---|
| **`L10` vs `L6`** | `1X2 RPS` | `-0.002815` | `[-0.004125, -0.001493]` | `[-0.004065, -0.001110]` | `1.0000` / `0.9985` |
| **`L10` vs `L6`** | `1X2 Log-Loss` | `-0.008001` | `[-0.011675, -0.004365]` | `[-0.011334, -0.003689]` | `1.0000` / `0.9990` |
| **`L10` vs `L6`** | `Over 2.5 Brier` | `-0.001614` | `[-0.002736, -0.000582]` | `—` | `0.9990` |
| **`L20` vs `L10`** | `1X2 RPS` | `-0.001270` | `[-0.002311, -0.000226]` | `[-0.002527, -0.000105]` | `0.9950` / `0.9810` |
| **`L20` vs `L10`** | `1X2 Log-Loss` | `-0.003549` | `[-0.006444, -0.000738]` | `[-0.006902, -0.000389]` | `0.9955` / `0.9815` |
| **`L20` vs `L6`** | `1X2 RPS` | **`-0.004084`** | **`[-0.005442, -0.002789]`** | **`[-0.004867, -0.003128]`** | **`1.0000` / `1.0000`** |
| **`L20` vs `L6`** | `1X2 Log-Loss` | **`-0.011550`** | **`[-0.015598, -0.007634]`** | **`[-0.013527, -0.009102]`** | **`1.0000` / `1.0000`** |
| **`L20` vs `L6`** | `1X2 Brier/salida` | **`-0.002816`** | **`[-0.003819, -0.001880]`** | `—` | **`1.0000`** |
| **`L20` vs `L6`** | `Over 2.5 Brier` | `-0.001516` | `[-0.003282, +0.000149]` | `—` | `0.9560` |
| **`L20` vs `L6`** | `BTTS Yes Brier` | `+0.000135` | `[-0.001406, +0.001294]` | `—` | `0.4340` |

**Conclusión estadística (A3):** **`L20` supera a `L6` de forma estadísticamente sólida y consistente en `1X2 RPS`, `1X2 Log-Loss` y `1X2 Brier`** tanto con bloques diarios como semanales (`IC 95%` completamente negativo, `p > 0.999`), y también mejora a `L10` en `1X2`. En totales `Over 2.5` la mejora frente a `L6` es direccional pero no alcanza significación estricta al 95% (y entre `L10` y `L20` es neutra), y en `BTTS` es estadísticamente indistinguible. **No** se ha utilizado el conjunto test para tunear hiperparámetros.

---

## 3. Aclaración de las `4,496` / `4,638` observaciones y separación epistémica del sesgo (A3)

- **Por qué sumaban `4,496` observaciones para `2,248` fixtures (`4,638` para `2,319`):** el bloque `favorite_underdog_bias` evalúa conjuntamente **ambas probabilidades direccionales por partido**: `P(home_win)` y `P(away_win)` (`2 × 2,248 = 4,496`; `2 × 2,319 = 4,638`), excluyendo `draw`. Por tanto no son `4,496` partidos independientes sino `2` puntas por partido (correlacionadas negativamente dentro de cada encuentro). Separando por bando en `L20` (`n = 2,319`):
  - `home_only_ge_0_55` (`n = 110`): predicha `0.5944` vs observada `0.7364` (sesgo `-0.1420`).
  - `away_only_ge_0_55` (`n = 24`): predicha `0.6003` vs observada `0.7500` (sesgo `-0.1497`).
  - Combinado `2n` (`n = 134`): predicha `0.5954` vs observada `0.7388` (sesgo `-0.1434`).
- **Distinción obligatoria:**
  1. **Sesgos demostrados OOS (`L20`, `n = 2,319`):** (a) subestimación de favoritos `p >= 0.55` (`-14.34 pp` en observaciones lado-partido; `-14.20 pp` en local solo, `-14.97 pp` en visitante solo); (b) sobrestimación leve de `< 0.35` (`+4.89 pp`); (c) subestimación de goles del local (`bias_home_pred_minus_obs = -0.1696`, `pred_mean(home_win) = 0.4096` vs `0.4644`); (d) **degradación de ECE en `home_win`** al pasar de `L6` (`0.0451`) a `L20` (`0.0557`): más muestra reduce varianza (mejor `RPS`/`Log-Loss`) pero mantiene compresión hacia el prior/aditividad.
  2. **Hipótesis causales (pendientes de ablación OOS en Iteración 2.1B):** la compresión podría deberse conjuntamente a: `shrink_k = 8.0` hacia priors planos, combinación aditiva `0.55*own + 0.45*opp` en vez de multiplicativa, ausencia de fuerza del rival (SoS/Elo) y ajuste insuficiente de localía (`0.25` venue blend). **No puede atribuirse toda la subestimación a la fórmula aditiva sin ejecutar la ablación controlada en 2.1B.**
  3. **Mejoras experimentales aún no validadas:** especificación multiplicativa/log-lineal, SoS/Elo as-of y recalibración posterior.

---

## 4. Auditoría de incertidumbre y cobertura real (`A4`)

- **Lo que `raw_bin_coverage = 1.0 (5/5)` NO mide:** en [`evaluate_interval_calibration`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py#L244-L323), `raw_bin_coverage = 1.0` únicamente comprueba que, tras agrupar predicciones en **5 quintiles** (`~464` partidos por bin), la tasa empírica media del quintil cae entre la media de `p_low` y la media de `p_high` del quintil. Como `calibration.py:321` advierte explícitamente (`RATE_BAND_BIN_CONTAINMENT_NOT_EQUATED_TO_FULL_CALIBRATION`), **eso NO acredita calibración de intervalos predictivos al 80%**.
- **Qué miden realmente las bandas `[p_lo, p_hi]`:** propagan de forma determinista por esquinas `[lh_lo, lh_hi] × [la_lo, la_hi]` intervalos Gamma Wilson-Hilferty (`z = 1.2816` sobre tasas medias, `n_post = (n_eff + 8.0) * 0.85 si mix`), produciendo bandas extremadamente anchas: en `L20` (`n = 2,319`), **`0.3285`** en `home_win`, **`0.3330`** en `over_2_5` y **`0.2646`** en `btts_yes` (`~26–33 pp`).
- **Cobertura real sobre realizaciones enteras `[q10, q90]` de goles totales (`count_interval_coverage_80`):** medida directamente en [BASELINE_REPRODUCIBLE.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json), la banda predictiva discreta `[q10, q90]` de goles totales alcanza **`0.8650`** en `L6`, **`0.8749`** en `L10` y **`0.8715`** en `L20` (por encima del 80% nominal debido a la discreción de conteos bajos y dispersión aplicada).
- **Procedimiento correcto para calibrar incertidumbre en 2.1B:** (1) evaluar PIT discreto (*randomized Probability Integral Transform*) sobre goles locales, visitantes y totales; (2) calibrar intervalos posteriores conjuntos mediante muestreo Monte Carlo/cuadratura sobre `(lambda_home, lambda_away)` verificando cobertura nominal empírica `[q10, q90]` cercana al 80% y nitidez (*sharpness*) sin sobreanchura artificial; y (3) medir calibración condicional de `p_low` antes de usarlo en `ev_lo > 0`.
