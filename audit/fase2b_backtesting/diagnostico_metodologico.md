# BET ENGINE V4 — FASE 2B.2: VALIDACIÓN METODOLÓGICA DEL BACKTESTING Y DIAGNÓSTICO DE SESGOS

**Fecha:** 2026-11-19  
**Rama:** `refactor/etapa2-fundamentos`  
**Baseline congelado evaluado:** `CoherentMatchModeler_baseline_2.0_2.1A`  
**Script diagnóstico aislado:** [audit_fase2b2_methodology.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/audit_fase2b2_methodology.py) → [audit_metrics.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/audit_metrics.json)  
**Restricción cumplida:** no se han modificado fórmulas, priors, pesos ni parámetros del modelo de producción ni expedientes PREMATCH cerrados.

---

## 1. Veredicto de validez del backtesting actual

| Alcance / Afirmación | Veredicto | Categoría epistémica | Sustento principal |
|---|---|---|---|
| **Comparación relativa en `GOALS_FT` / `1X2` / `Over 2.5` / `BTTS` sobre `split_strict`** (`n = 2,323` PREMATCH o `2,319` sin rojas) | **VÁLIDO CON RESERVAS METODOLÓGICAS** | **Condicionado** *(temporalidad estricta y sin empate de frontera, pero con `available_at_utc` sintético y `test_strict` ya inspeccionado)* | Las ventanas previas respetan `kickoff < t_ko`; el sesgo observado es consistente en `train_strict` y `test_strict` con bootstrap diario y semanal. |
| **Detección de sesgos direccionales (localía + compresión de favoritos)** | **SISTEMÁTICO Y ESTADÍSTICAMENTE RESPALDADO** | **Certificado como diagnóstico sobre corpus histórico** *(pendiente de confirmación en muestra prospectiva virgen)* | La infrapredicción de goles locales (`lambda_home - hg`) y de favoritos locales y visitantes persiste entre estratos y bloques temporales (`IC 95 %` excluye `0`). |
| **Evaluación mediante `WalkForwardEvaluator.evaluate_joint_match_walk_forward` / `evaluate_window_depth_and_weighting`** | **PARCIALMENTE INVÁLIDO COMO HOLDING OOS** | **Exploratorio / Defectuoso metodológicamente** | Excluye post-kickoff por tarjeta roja objetivo, mezcla todo el periodo sin partición train/test, descarta reconstrucciones o `min_prior=6..9`, y usa `max_goals=10`. |
| **Extrapolación a expedientes PREMATCH con `L20` completo** | **NO VÁLIDO** | **Exploratorio insuficiente (`n = 50`)** | Solo `50` partidos tienen `>= 20` antecedentes limpios por bando; `0` están en `train`, `62 %` son reconstruidos unilateralmente y todos ocurren en 7 días de noviembre. |
| **Evaluación de mercados secundarios (`CORNERS`, `SHOTS`, `ON_TARGET`, `FOULS`, `YELLOW_CARDS`, `GOALS_H1`) y rentabilidad (`ROI`, `EV`, `CLV`)** | **NO VÁLIDO / SIN ACREDITACIÓN** | **No acreditado** | `0/4,496` partidos tienen cuotas Bwin vinculadas y apenas `9–143` cumplen cobertura secundaria bilateral `min6`. |

**Conclusión ejecutiva de validez:** el backtesting estricto (`reproduce_baseline_backtest.py` sobre `split_strict`) sí es suficientemente fiable para confirmar que el modelo baseline tiene un **doble sesgo estructural real** —infravalora sistemáticamente la ventaja local en goles esperados (`lambda_home`) y comprime demasiado hacia la media las probabilidades de favoritos tanto locales como visitantes—; sin embargo, **no debe usarse tal cual como juez único para calibración ni para promoción automática** hasta corregir las 5 deficiencias metodológicas detalladas abajo y evaluar sobre el holdout prospectivo virgen.

---

## 2. Problemas metodológicos confirmados y sus efectos

### 2.1. Tratamiento de tarjetas rojas (`rc_target` y `RED_CARD_UNKNOWN`) — **Fuga retrospectiva confirmada + asimetría extrema de cobertura**

1. **Exclusión post-kickoff en evaluadores:**
   - [reproduce_baseline_backtest.py:441](file:///Users/antonio/Projects/bet-tactical-engine/tools/reproduce_baseline_backtest.py#L441) excluye los `7` partidos objetivo con `rc_target == True` (`4,496 -> 4,489`).
   - [`WalkForwardEvaluator.evaluate_joint_match_walk_forward`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py#L960), [`evaluate_window_depth_and_weighting`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py#L1352) y [`evaluate_single_team_metric_windows`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py#L1687) filtran del mismo modo si el partido **objetivo** registró expulsión. En PREMATCH esa información no existe antes del inicio.
2. **Subregistro masivo silencioso (`RED_CARD_UNKNOWN` tratado como `False`):**
   - De **`34,723`** filas crudas en `statistical_observations` de los 10 JSON, **solo `78` (`0.22 %`)** contienen `own.red` no nulo (`0` con `league_name` o `country` en la fila histórica).
   - En los **`4,496`** elegibles (`min >= 6`), **solo `39` (`0.87 %`; `8` en train y `31` en test)** tienen `red_card_verified == True`; los **`4,457` restantes (`99.13 %`)** quedan en `RED_CARD_UNKNOWN`, pero [ObservationStore.from_preselection_match](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L1093) asigna `red_card_distortion = False` igualmente.
   - **Los `7` excluidos por `rc_target` pertenecen en su totalidad a `NATIONAL_TEAM`** (`3` en train: `1528865`, `1528878`, `1528884`; `4` en test: `1528920` `2-1`, `1528928` `6-0`, `1528941` `2-1`, `1528971` `0-0`), lo que supone un **descenso selectivo del `17.95 %` (`7/39`)** sobre partidos con estadística verificada frente a `0 %` en clubes sin estadística de rojas.
3. **Efecto cuantificado (Principal PREMATCH con rojas vs Secundario sin rojas):**
   - En `test` global (`n = 2,323` vs `2,319`), la diferencia agregada es marginal (`RPS = 0.21824` vs `0.21823`; sesgo `home_win = -5.54 pp` vs `-5.48 pp`).
   - En cambio, en **`NATIONAL_TEAM` (`n = 51` vs `47`)**, excluir los `4` encuentros distorsiona fuertemente el segmento: `home_win obs_rate` cae de **`41.18 %` (`bias = -0.89 pp`)** a **`38.30 %` (`bias = +2.02 pp`)**, cambiando incluso el signo del sesgo local (`goals bias_home` pasa de `+0.1535` a `+0.2509`).

### 2.2. Dependencia de orden de lectura y bug `"UNKNOWN" == "UNKNOWN"` en `competition_stratum` — **Defecto de pipeline confirmado**

- En [ObservationStore.from_preselection_match](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L1064), `lname` toma `target["league"]` **únicamente si `row["league_id"] == target["league_id"]`**; de lo contrario queda `""` y `_classify_stratum` clasifica el partido histórico como `"UNKNOWN"`.
- Al construir `team_series[tid][fid] = hm` en [reproduce_baseline_backtest.py:88](file:///Users/antonio/Projects/bet-tactical-engine/tools/reproduce_baseline_backtest.py#L88), **`440` de `2,785`** registros `(team_id, fixture_id)` presentes en múltiples bundles **cambian su `competition_stratum` / `league_name` según el orden de iteración de archivos**.
- Más grave: en [`_estimate_team_rate_detailed`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py#L464), `if target_stratum and m.competition_stratum == target_stratum: return 0.50` trata el literal `"UNKNOWN"` como verdadero. Por ello, **`791` de `978` (`80.88 %`) partidos objetivo con estrato `UNKNOWN`** ponderan partidos previos de **cualquier otra liga distinta también etiquetada `"UNKNOWN"` a `0.50` en lugar de `0.25`**.

### 2.3. Timestamps sintéticos `available_at_utc` y trazabilidad temporal

- `ObservationStore.from_preselection_match` ([line 1051](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L1051)) calcula `available_at_utc = kickoff + 125 min` mediante `_estimate_available_at_utc`; no existe marca temporal real de ingesta API en los `10` JSON.
- Aunque el orden relativo (`kickoff < t_ko`, `lag` respecto a `generated_at_utc` de `0.69` a `549.86` días, mediana `68.32` días, `0` negativos) garantiza ausencia de fuga en `split_strict`, la ausencia de hora real de finalización/publicación impide certificar partidos jugados en la misma ventana de `< 3` horas si existieran retrasos o prórrogas.

### 2.4. Divergencias metodológicas de `WalkForwardEvaluator` frente a `reproduce_baseline_backtest.py` y producción

1. [`evaluate_joint_match_walk_forward`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py#L930-L1076): requiere `BOTH_SIDES` explícitos (`n = 4,411`), excluye `rc_target`, evalúa sin división train/test, usa `max_goals=10` (vs `12` en [`CoherentMatchModeler.build_model`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py#L702)), y omite `kickoff_utc < t_ko` explícito apoyándose solo en `available_at_utc <= t_ko`.
2. [`evaluate_window_depth_and_weighting`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py#L1229-L1648): excluye `rc_target`, exige `min_prior_per_team=10` por defecto (descartando los `1,354` partidos `6..9`), usa `max_goals=10` con clipping `[0.001, 0.999]`, y no separa conjunto de ajuste de conjunto OOS.
3. [`audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json): su `code_hashes["bet_engine/context/observation_store.py"]` (`c9223b...`) quedó obsoleto tras las ediciones de Fase 2A.1/2A.1B (`245230bb2fa01225ee9f12f780a580e63fe7a998095cfe5ba246f0dbd41cc78c`), lo que provoca **`MANIFEST_CODE_HASH_MISMATCH`** en 3 tests de `test_iteracion_2_2c_registry_and_elo` si se invoca `prospective_registry.py` sin actualizar el manifiesto.

### 2.5. Contaminación por inspección previa de `test_strict` y selección retrospectiva del corpus

- Las `2,319` (`2,323` con `rc`) filas de `test_strict` han sido examinadas repetidamente en `2.0`, `2.1A`, `2.1B`, `2.2A` y en este diagnóstico; usar esa misma partición para elegir hiperparámetros causaría sobreajuste al test.
- El corpus (`23,063` embebidos) procede retrospectivamente de historiales `L20` de `878` targets capturados entre `2026-11-02` y `2026-11-17` (`117` targets terminaron incorporándose posteriormente como observaciones históricas en capturas posteriores). No es una muestra aleatoria anual ni representa todas las jornadas de cada competición.

---

## 3. Conjunto principal PREMATCH y conjuntos de sensibilidad

| Conjunto | Definición | Train (`< 2026-09-26T14:00Z`) | Test (`>= 2026-09-26T14:00Z`) | Total | Rol metodológico |
|---|---|---:|---:|---:|---|
| **Principal PREMATCH (`PREMATCH_CLEAN_HISTORY_WITH_TARGET_RC`)** | Historial previo limpio (`available_at_utc <= t_ko`, `kickoff < t_ko`, sin roja previa conocida) y `min >= 6`, **sin excluir `rc_target`** | **2,173** | **2,323** | **4,496** | Evaluación principal representativa de PREMATCH (sin selección futura por expulsión objetivo). |
| **Secundario `NO_TARGET_RED_CARD`** | Igual que el principal excluyendo los `7` partidos con roja confirmada en el partido objetivo (`3` train, `4` test) | **2,170** | **2,319** | **4,489** | Análisis secundario condicionado (solo referencia; sesgado por `99.13 %` de `RED_CARD_UNKNOWN`). |
| **Sensibilidad `BOTH_SIDES` puro** | Solo partidos observados en ambos bandos (`min >= 6`, con o sin `rc_target`) | 2,173 *(incl. 0 recons.)* | 2,245 | 4,418 | Controla sesgo de reconstrucción unilateral (`78` partidos en test). |
| **Sensibilidad `DEPTH` y `STRATUM`** | Subconjuntos `min 6..9` (`1,354`), `min 10..19` (`3,092`), `min >= 20` (`50`), y `DOMESTIC_LEAGUE` vs `UNKNOWN` vs `NATIONAL_TEAM` | — | — | — | Aísla efectos de profundidad, mezcla competitiva y estrato. |

---

## 4. Métricas por segmentos con tamaños de muestra (`CoherentMatchModeler_baseline_2.0_2.1A`, `L20` sobre conjunto Principal PREMATCH)

### 4.1. Estabilidad temporal (`Train` vs `Test` vs `Full`) y comparación `L6 / L10 / L20`

| Partición / Ventana | `n` | `RPS` | `LogLoss 1X2` | `P(1)` / `Obs(1)` (`Sesgo 1`) | `P(X)` / `Obs(X)` (`Sesgo X`) | `P(2)` / `Obs(2)` (`Sesgo 2`) | `Sesgo O2.5` | `Sesgo λ_H` | `Sesgo λ_A` | `IC 95 % bloque diario Sesgo 1` |
|---|---:|---:|---:|---|---|---|---|---|---|---|
| **`Train` (`L20`, `< 26/09`)** | **2,173** | 0.22174 | 1.03824 | 0.4138 / 0.4386 (**-2.47 pp**) | 0.2561 / 0.2402 (+1.59 pp) | 0.3301 / 0.3212 (+0.88 pp) | -1.90 pp | **-0.0955** | -0.0060 | **[-4.15, -0.81 pp]** |
| **`Test` (`L20`, `>= 26/09`)** | **2,323** | 0.21824 | 1.02078 | 0.4095 / 0.4649 (**-5.54 pp**) | 0.2524 / 0.2286 (+2.38 pp) | 0.3381 / 0.3065 (+3.16 pp) | **-3.79 pp** | **-0.1711** | +0.0087 | **[-7.34, -3.60 pp]** |
| **`Full` (`L20`, 4,496)** | **4,496** | 0.21993 | 1.02922 | 0.4116 / 0.4522 (**-4.06 pp**) | 0.2542 / 0.2342 (+2.00 pp) | 0.3342 / 0.3136 (+2.06 pp) | **-2.88 pp** | **-0.1345** | +0.0016 | **[-5.39, -2.74 pp]** |
| `Test` (`L6`) | 2,323 | 0.22233 | 1.03234 | 0.4070 / 0.4649 (-5.79 pp) | 0.2491 / 0.2286 (+2.05 pp) | 0.3439 / 0.3065 (+3.74 pp) | -3.63 pp | -0.1757 | +0.0110 | *L20−L6 ΔRPS = -0.00409* |
| `Test` (`L10`) | 2,323 | 0.21950 | 1.02431 | 0.4084 / 0.4649 (-5.65 pp) | 0.2512 / 0.2286 (+2.26 pp) | 0.3404 / 0.3065 (+3.39 pp) | -3.63 pp | -0.1743 | +0.0149 | *L20−L10 ΔRPS = -0.00127* |

- **Bootstrap semanal (`8` bloques en `test`, `n = 2,323`):** `Sesgo 1 = -5.54 pp` (`IC [-6.90, -4.32 pp]`), `Sesgo X = +2.38 pp` (`IC [+0.88, +4.76 pp]`), `Sesgo 2 = +3.16 pp` (`IC [+0.90, +4.92 pp]`), `Sesgo λ_H = -0.1711` (`IC [-0.2054, -0.1197]`), `Sesgo O2.5 = -3.79 pp` (`IC [-5.26, -2.29 pp]`).
- **Comparación pareada `L20` vs `L6` / `L10` (`n = 2,323`):** `L20` mejora a `L6` en `RPS` (`-0.004091`, `IC [-0.005417, -0.002823]`, `p = 1.0000`) y `LogLoss 1X2` (`-0.011559`, `IC [-0.015587, -0.007736]`); también supera a `L10` en `RPS` (`-0.001265`, `IC [-0.002307, -0.000231]`, `p = 0.9950`) y `LogLoss 1X2` (`-0.003527`, `IC [-0.006443, -0.000732]`, `p = 0.9955`).

### 4.2. Por estrato competitivo, temporada, mezcla y reconstrucción (`L20`)

| Segmento (`Test`, `n = 2,323`, salvo indicación) | `n` | `RPS` | `LogLoss` | `Sesgo 1` (`P − Obs`) | `IC 95 % Sesgo 1` | `Sesgo 2` | `Sesgo λ_H` | `Sesgo λ_A` |
|---|---:|---:|---:|---|---|---:|---:|---:|
| **`DOMESTIC_LEAGUE`** (`Test`) | **1,701** | 0.21659 | 1.02876 | **-4.83 pp** (0.4073 vs 0.4556) | **[-6.79, -2.71 pp]** | **+4.05 pp** | **-0.1647** | +0.0209 |
| **`UNKNOWN` (sin `league_name`)** (`Test`) | **521** | 0.22399 | 0.99714 | **-8.02 pp** (0.4170 vs 0.4971) | **[-11.52, -4.68 pp]** | +0.51 pp *(X: +7.50 pp)* | **-0.1984** | +0.0060 |
| **`NATIONAL_TEAM`** (`Test`, incluye 4 `rc_target`) | **51** | 0.20613 | 0.97837 | -0.89 pp (0.4029 vs 0.4118) | [-10.37, +8.73 pp] | -3.00 pp | +0.1535 | -0.0615 |
| **`DOMESTIC_CUP`** (`Test`) | **34** | 0.23012 | 0.99238 | -17.07 pp (0.4175 vs 0.5882) | *n < 40 (exploratorio)* | +7.74 pp | -0.5686 | -0.1641 |
| **`CONTINENTAL_CLUB`** (`Test`) | **16** | 0.22057 | 1.13809 | +9.93 pp (0.4118 vs 0.3125) | *n < 40 (exploratorio)* | +3.49 pp | -0.1508 | -0.6110 |
| **Temporada `2026`** (`Test`) | **2,303** | 0.21810 | 1.02055 | **-5.49 pp** | **[-7.31, -3.48 pp]** | **+3.14 pp** | **-0.1753** | +0.0074 |
| **Temporada `2025`** (`Full`) | **296** | 0.22095 | 1.03963 | -1.24 pp (0.4031 vs 0.4155) | [-6.51, +3.86 pp] | -0.82 pp | -0.0156 | -0.0151 |
| **Temporada `2027`** (`Test`, estatal/Nigeria/Camerún) | **20** | 0.23504 | 1.04711 | -10.97 pp (0.4403 vs 0.5500) | *n < 40 (exploratorio)* | +5.09 pp | +0.3149 | +0.1601 |
| **`MIXED_COMP` (`COMPETITION_MIX_FALLBACK`)** (`Test`) | **2,263** | 0.21776 | 1.02065 | **-5.45 pp** | **[-7.23, -3.50 pp]** | **+3.32 pp** | **-0.1706** | +0.0084 |
| **`PURE_SAME_LEAGUE`** (`Test`) | **60** | 0.23636 | 1.02596 | -8.84 pp (0.3949 vs 0.4833) | [-22.44, +1.76 pp] | -2.97 pp | -0.1875 | +0.0192 |
| **`H_VB + A_VB` (`>= 3` en sede ambos)** (`Test`) | **2,314** | 0.21826 | 1.02086 | **-5.59 pp** | **[-7.42, -3.64 pp]** | **+3.21 pp** | **-0.1713** | +0.0126 |
| **`BOTH_SIDES`** (`Test`) | **2,245** | 0.21808 | 1.02085 | **-5.46 pp** | **[-7.32, -3.46 pp]** | **+3.14 pp** | **-0.1721** | +0.0102 |
| **`RECONSTRUCTED_FROM_HOME`** (`Test`) | **46** | 0.21681 | 1.00639 | -10.04 pp (0.4213 vs 0.5217) | [-20.58, +17.84 pp] | +7.04 pp | -0.0834 | +0.1844 |
| **`RECONSTRUCTED_FROM_AWAY`** (`Test`) | **32** | 0.23183 | 1.03667 | -4.82 pp (0.3893 vs 0.4375) | *n < 40* | -1.49 pp | -0.2243 | -0.3490 |

### 4.3. Por diferencia de fuerza esperada (`λ_H − λ_A`) y probabilidad propia del modelo (separando `P(1)` y `P(2)` sin doble conteo)

| Eje de evaluación (`Test`, `n = 2,323`) | `n` | `RPS` | `LogLoss` | `P(1) / Obs(1)` (`Sesgo 1`) | `P(2) / Obs(2)` (`Sesgo 2`) | `Sesgo λ_H` | `Sesgo λ_A` | `IC 95 % Sesgo del bando dominante` |
|---|---:|---:|---:|---|---|---:|---:|---|
| **`λ_H − λ_A < -0.50`** (Visitante fuerte) | **99** | 0.18308 | 0.87306 | 0.2373 / 0.1414 (**+9.58 pp**) | 0.5332 / 0.6970 (**-16.38 pp**) | **+0.3408** | **-0.5580** | `Sesgo 2`: **[-27.99, -6.16 pp]** |
| **`λ_H − λ_A ∈ [-0.50, -0.15)`** (Visitante leve) | **337** | 0.22934 | 1.05981 | 0.3098 / 0.3205 (-1.07 pp) | 0.4369 / 0.4362 (+0.07 pp) | -0.0280 | -0.0788 | `Sesgo 2`: [-4.85, +4.71 pp] |
| **`λ_H − λ_A ∈ [-0.15, +0.15]`** (Equilibrado) | **679** | 0.23305 | 1.07866 | 0.3716 / 0.3991 (-2.75 pp) | 0.3668 / 0.3490 (+1.78 pp) | -0.0941 | -0.0310 | `Sesgo 1`: [-7.30, +1.85 pp] |
| **`λ_H − λ_A ∈ (+0.15, +0.50]`** (Local leve) | **792** | 0.21929 | 1.02424 | 0.4399 / 0.5379 (**-9.79 pp**) | 0.3031 / 0.2336 (**+6.95 pp**) | **-0.2589** | +0.0830 | `Sesgo 1`: **[-13.67, -5.62 pp]** |
| **`λ_H − λ_A > +0.50`** (Local fuerte) | **416** | 0.19146 | 0.92327 | 0.5354 / 0.6274 (**-9.20 pp**) | 0.2313 / 0.1779 (**+5.34 pp**) | **-0.3671** | +0.1377 | `Sesgo 1`: **[-13.01, -4.80 pp]** |
| **`P(home_win) < 0.25`** | **60** | 0.17098 | 0.83974 | 0.2183 / 0.1167 (**+10.17 pp**) | 0.5514 / 0.7167 (**-16.52 pp**) | +0.3384 | -0.7681 | `Sesgo 1`: **[+2.16, +17.14 pp]** |
| **`P(home_win) ∈ [0.25, 0.35)`** | **492** | 0.22736 | 1.05558 | 0.3131 / 0.3171 (-0.39 pp) | 0.4292 / 0.4390 (-0.98 pp) | -0.0014 | -0.0812 | `Sesgo 1`: [-4.78, +3.17 pp] |
| **`P(home_win) ∈ [0.35, 0.45)`** | **1,071** | 0.22887 | 1.06141 | 0.4002 / 0.4538 (**-5.36 pp**) | 0.3402 / 0.3016 (**+3.87 pp**) | -0.1633 | +0.0270 | `Sesgo 1`: **[-8.02, -2.14 pp]** |
| **`P(home_win) ∈ [0.45, 0.55)`** | **590** | 0.20817 | 0.97870 | 0.4918 / 0.5932 (**-10.14 pp**) | 0.2639 / 0.2017 (**+6.22 pp**) | -0.2934 | +0.0725 | `Sesgo 1`: **[-14.02, -6.42 pp]** |
| **`P(home_win) >= 0.55`** | **110** | 0.15386 | 0.79400 | 0.5944 / 0.7364 (**-14.20 pp**) | 0.1906 / 0.1000 (**+9.06 pp**) | -0.6268 | +0.3130 | `Sesgo 1`: **[-23.79, -3.12 pp]** |
| **`P(away_win) >= 0.55`** | **24** | 0.14770 | 0.77115 | 0.1917 / 0.0833 (+10.84 pp) | 0.6003 / 0.7500 (**-14.97 pp**) | +0.5853 | -1.0200 | *n < 40 (exploratorio)* |
| **`P(away_win) ∈ [0.45, 0.55)`** | **181** | 0.20807 | 0.96806 | 0.2737 / 0.2044 (**+6.93 pp**) | 0.4852 / 0.5967 (**-11.14 pp**) | +0.2111 | -0.3313 | `Sesgo 2`: **[-17.82, -6.02 pp]** |

*(Nota: en `full_4496`, `λ_H − λ_A < -0.50` con `n = 164` replica exactamente el mismo giro: `Sesgo 2 = -13.09 pp`, `IC [-20.88, -5.49 pp]`; `Sesgo 1 = +7.12 pp`, `IC [+1.84, +12.30 pp]`).*

### 4.4. Por profundidad histórica disponible (`min_prior`) y estabilidad mensual

| Profundidad / Mes | `n` (`Test`) | `n` (`Full`) | `RPS` | `P(1) / Obs(1)` (`Sesgo 1`) | `Sesgo O2.5` | `Sesgo λ_H` | `Sesgo λ_A` | Observación clave |
|---|---:|---:|---:|---|---:|---:|---:|---|
| **`min 6..9`** | 63 *(Test)* | **1,354** | 0.22014 *(Full)* | 0.4169 / 0.4557 (**-3.88 pp** `Full`) | -2.41 pp `Full` | -0.1386 `Full` | +0.0005 `Full` | `IC 95 % Sesgo 1 Full`: **[-6.54, -1.40 pp]** |
| **`min 10..19`** | 2,210 *(Test)* | **3,092** | 0.22015 *(Full)* | 0.4094 / 0.4505 (**-4.11 pp** `Full`) | -3.17 pp `Full` | -0.1367 `Full` | +0.0003 `Full` | `IC 95 % Sesgo 1 Full`: **[-5.86, -2.25 pp]** |
| **`min >= 20`** | **50** *(Test)* | **50** | 0.20083 | 0.4086 / 0.4600 (-5.14 pp) | +2.02 pp | **+0.1097** | **+0.1071** | **No extrapolable** (`62 %` reconstruidos, 7 fechas de nov.) |
| **`2026-09` (`>= 26/09`)** | 262 | 1,429 | 0.22041 | 0.4153 / 0.4466 (-3.12 pp) | -5.72 pp | -0.1292 | -0.0872 | Solo `5` días en `test` (`IC [-9.66, +9.51 pp]`) |
| **`2026-10`** | 1,782 | 1,782 | 0.21794 | 0.4092 / 0.4691 (**-5.99 pp**) | -3.73 pp | **-0.1819** | +0.0284 | `IC 95 % Sesgo 1`: **[-7.88, -3.73 pp]** |
| **`2026-11`** | 279 | 279 | 0.21814 | 0.4061 / 0.4552 (-4.91 pp) | -2.40 pp | **-0.1409** | -0.0270 | `IC 95 % Sesgo λ_H`: **[-0.2413, -0.0474]** |

---

## 5. Sesgos estadísticamente respaldados vs hallazgos exploratorios

### 5.1. Sesgos estadísticamente respaldados (distinguibles del ruido en múltiples particiones/bloques)

1. **Compresión bidireccional hacia la media (sesgo de favoritos y no favoritos):**
   - El modelo genera probabilidades demasiado planas alrededor de `(0.41 / 0.25 / 0.34)`: solo clasifica `110` locales y `24` visitantes con `P >= 0.55` en `n = 2,323`.
   - Cuando el modelo detecta superioridad clara de cualquiera de los dos bandos, infrapredice al favorito —**`-14.20 pp` para `P(1) >= 0.55` (`n = 110`)**, **`-11.14 pp` para `P(2) ∈ [0.45, 0.55)` (`n = 181`)**, y **`-16.38 pp` en `λ_H − λ_A < -0.50` (`n = 99`)**— y sobrepredice al underdog (`+9.58 pp` al local débil en `λ_H − λ_A < -0.50`).
   - Causa estructural plausible en código: doble contracción `shrink_k = 8.0` tanto en ataque como en defensa concedida + ausencia de ajuste de fuerza del rival (Elo/SoS) + mezcla `0.75` global / `0.25` de sede.
2. **Infrapredicción sistemática de `lambda_home` y victoria local agregada (`home_win`):**
   - Aparece tanto en `train` (**`Sesgo λ_H = -0.0955`**, `IC [-0.1560, -0.0327]`; **`Sesgo 1 = -2.47 pp`**, `IC [-4.15, -0.81 pp]`) como en `test` (**`Sesgo λ_H = -0.1711`**, `IC [-0.2176, -0.1216]`; **`Sesgo 1 = -5.54 pp`**, `IC [-7.34, -3.60 pp]`), mientras `lambda_away` se mantiene centrado (`-0.0060` en train, `+0.0087` en test).
   - Se mantiene dentro de `DOMESTIC_LEAGUE` (`-4.83 pp`), `UNKNOWN` (`-8.02 pp`), `BOTH_SIDES` (`-5.46 pp`), `min 6..9` (`-3.88 pp` full) y `min 10..19` (`-4.11 pp` full).
   - Como consecuencia matemática, el modelo también infrapredice `Over 2.5` (`-3.79 pp` en test, `IC [-5.56, -1.98 pp]`) y sobrepredice `draw` (`+2.38 pp`) y `away_win` (`+3.16 pp`).
3. **Superioridad de `L20` frente a `L6` y `L10` sobre este estimador:**
   - Confirmada por block bootstrap diario pareado sobre `n = 2,323` (`ΔRPS L20−L6 = -0.004091`; `ΔRPS L20−L10 = -0.001265`), probablemente porque amortigua el ruido muestral bajo `shrink_k = 8.0`, aunque no corrige ninguno de los dos sesgos estructurales anteriores.

### 5.2. Hallazgos todavía exploratorios (muestra insuficiente o IC compatible con cero)

- **Estratos pequeños y ligas individuales:** `NATIONAL_TEAM` (`n = 51` test, `IC Sesgo 1 [-10.37, +8.73 pp]`), `DOMESTIC_CUP` (`n = 34`), `CONTINENTAL_CLUB` (`n = 16`), temporada `2027` (`n = 20`) y `PURE_SAME_LEAGUE` (`n = 60`). En ligas y países individuales solo `England` (`n = 867` full: `-6.97 pp`, `IC [-9.96, -3.84 pp]`) y ligas puntuales como `FA Cup` (`n = 72`: `-18.14 pp`) o `Switzerland` (`n = 96`: `-12.19 pp`) superan ruido en esta muestra, con claro riesgo de multiplicidad estadística.
- **`min >= 20` (`n = 50`):** aunque exhibe `-5.14 pp` en `home_win`, sobreestima goles locales (`+0.1097`) y visitantes (`+0.1071`) y tiene `IC` amplísimo (`Sesgo λ_H [-0.008, +0.430]`), reflejando composición atípica de ligas menores/femeninas de noviembre.

---

## 6. Representatividad y limitaciones por datos sintéticos o no verificables

1. **Los `50` encuentros con profundidad `>= 20` no representan expedientes PREMATCH L20:**
   - Distribución temporal: **`0` en train, `50` en test**, concentrados en 7 días (`2026-11-02` a `2026-11-10`).
   - Reconstrucción: **`31` de `50` (`62.0 %`) son reconstruidos unilateralmente** (`17` `RECONSTRUCTED_FROM_HOME` + `14` `RECONSTRUCTED_FROM_AWAY`) frente a solo `19` `BOTH_SIDES`, porque solo equipos recapturados en múltiples ventanas de noviembre acumularon 20–21 antecedentes en sus historiales `L20`.
   - Composición: `99` equipos en `26` ligas (p. ej. `8` de `Non League Premier - Southern Central`, `4` `Primera A`, `3` `National 2 - Group C`, además de `La Liga`: `1`, ligas femeninas y `Tercera RFEF`). **Prohibido extrapolar sus métricas (`RPS = 0.20083`, `bias λ_H = +0.1097`) al rendimiento PREMATCH `L20`.**
2. **Diferencia estructural entre `>= 6`, `>= 10` y concentración del corpus `4,496`:**
   - Volumen por tramos: `1,354` con `min 6..9` (`95.35 %` en train), `3,092` con `min 10..19` (`2,210` en test) y `50` con `min >= 20`.
   - **Concentración temporal severa:** el **`71.42 %`** de `4,496` (`1,429` en `2026-09` + `1,782` en `2026-10` = `3,211`) se concentra en solo dos meses; `2025-10..2026-06` suma apenas `340` partidos.
   - **Concentración geográfica/ligas:** `1,429` equipos y `269` ligas distintas; las 10 primeras ligas acumulan el **`26.71 %`** (`1,201 / 4,496`), lideradas por `England` (`867`, `19.28 %`), `NO_TARGET_COUNTRY` (`668`), `Scotland` (`251`), `Sweden` (`204`), `USA` (`203`), `Spain` (`189`). Ningún equipo supera `15` apariciones.
   - **Sesgo de descubrimiento retrospectivo:** al reconstruir el pasado desde `L20` de partidos de noviembre, los meses tempranos solo retienen equipos con calendarios densos o recapturas cruzadas; y los partidos de noviembre favorecen ligas activas tras parones/cierres de temporada.
3. **Datos sintéticos y no verificables:**
   - `100 %` de `available_at_utc` (`kickoff + 125 min`) es estimado sintéticamente.
   - `99.13 %` (`4,457 / 4,496`) carece de dato real de tarjeta roja (`RED_CARD_UNKNOWN`).
   - `21.75 %` (`978 / 4,496`) tiene estrato `UNKNOWN` por falta de `league_name` en observaciones históricas.
   - `0 %` de filas cuenta con cuota Bwin histórica ni alineación XI prepartido.

---

## 7. Protocolo para comparar futuras modificaciones sin sobreajustar

1. **Bloqueo del `test_strict` como conjunto explorado (`DIAGNOSTIC_INSPECTED`):**
   - El bloque `2026-09-26T14:00:00+00:00 .. 2026-11-14T19:45:00+00:00` (`2,323` PREMATCH / `2,319` sin roja) **queda congelado como diagnóstico de Fase 2B.2**; queda prohibido buscar hiperparámetros (`shrink_k`, pesos de sede, `rho`, ponderación Elo) iterando sobre sus resultados.
2. **Desarrollo y selección mediante validación cronológica anidada dentro de `train_strict` (`n = 2,173`):**
   - Toda calibración empírica (p. ej. tasas base por estrato, ajuste de ventaja local o ponderación SoS/Elo) debe ajustarse exclusivamente sobre `<= T_corte` en **folds cronológicos anidados dentro de `train_strict`** sin empates de kickoff:
     - **Fold anidado 1 (`cut = 2026-09-05T11:30:00+00:00`):** `train = 1,083`, `val = 1,090` (baseline `L20`: `Sesgo 1 = -2.50 pp`, `IC [-4.82, +0.51 pp]`, `Sesgo λ_H = -0.0975`).
     - **Fold anidado 2 (`cut = 2026-09-13T10:00:00+00:00`):** `train = 1,628`, `val = 545` (baseline `L20`: `Sesgo 1 = -1.06 pp`, `Sesgo λ_H = -0.0259`).
   - Una vez congelados los candidatos en validación anidada de `train_strict`, se permite **una única pasada de comprobación** sobre el conjunto principal de `test_strict` (`n = 2,323`) sin reajuste posterior.
3. **Decisión definitiva sobre holdout prospectivo virgen (`kickoff_utc > 2026-11-14T19:45:00+00:00`):**
   - Se evaluará mediante [prospective_registry.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/prospective_registry.py) con predicción pre-kickoff inmutable y liquidación separada post-FT, exigiendo `N >= 200` partidos `CLUB` (`NATIONAL_TEAM >= 50` por separado) y superioridad pareada (`IC` block bootstrap diario) en `RPS` y `LogLoss 1X2` sin degradar `Over 2.5` ni `BTTS`, reduciendo `Sesgo 1` y la compresión en `P >= 0.45`.
   - Hasta completar ese holdout prospectivo con cuotas reales (`ODDS_VERIFIED`), **ningún modelo puede emitir acreditación financiera ni autorización de apuesta**.
4. **Registro congelado de hashes y parámetros actuales:**
   - Modelo: `CoherentMatchModeler_baseline_2.0_2.1A` (`DEFAULT_PRIORS["goals"] = (1.56, 1.26)`, `STRATUM_PRIORS["NATIONAL_TEAM"]["goals"] = (1.50, 1.40)`, `shrink_k = 8.0`, `phi_k = 12.0`, `own_opp = (0.55, 0.45)`, `venue_blend = 0.25`, `L6/L20 = 0.35/0.65`, `rho = -0.04`, `max_goals = 12`).
   - SHA-256 verificados en rama:
     - [distributions.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py): `b85d7ab092217833234f53f0b9d90b56705c115f4b9a373a25e282ef5d5d8276`
     - [calibration.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py): `280aeec11dab05c2b9ee5d566af9ae6e99d10585cc35e25127c48756aa970097`
     - [observation_store.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py): `245230bb2fa01225ee9f12f780a580e63fe7a998095cfe5ba246f0dbd41cc78c`
     - [reproduce_baseline_backtest.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/reproduce_baseline_backtest.py): `5b98386ba76d46bf60ebc3425635871c48aee6ca7098db72c9b780d4a7d41038`

---

## 8. Correcciones mínimas necesarias antes de iniciar experimentos A/B

1. **Separar la evaluación principal PREMATCH del filtro post-kickoff de tarjeta roja:** evaluar por defecto sobre `4,496` (`2,173` train / `2,323` test) en `reproduce_baseline_backtest.py` y `WalkForwardEvaluator`, dejando `4,489` únicamente como reporte secundario etiquetado.
2. **Corregir la resolución de `competition_stratum` e independencia del orden:**
   - Construir un catálogo completo `league_id -> (league_name, league_type, country)` previo al parseo en `reproduce_baseline_backtest.py` / `ObservationStore` y evitar que bundles posteriores degraden a `"UNKNOWN"` un fixture ya clasificado (`440` sobrescrituras detectadas).
   - Corregir [`_estimate_team_rate_detailed`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py#L464) para que `target_stratum == "UNKNOWN"` **no** iguale a otros partidos `"UNKNOWN"` con peso `0.50` (`791` de `978` casos afectados), sino `0.25`.
3. **Alinear `WalkForwardEvaluator` con el motor de producción:** usar `max_goals=12` sin clipping `[0.001, 0.999]` en evaluaciones conjuntas, añadir `kickoff_utc < t_ko` explícito en `evaluate_joint_match_walk_forward` y soportar la partición cronológica `train` / `test` sin descartar `min 6..9`.
4. **Actualizar el manifiesto prospectivo tras los cambios de Fase 2A:** generar un nuevo manifiesto congelado (ej. `MANIFEST_MODELOS_CONGELADOS_V2.json`) con el SHA-256 vigente de `observation_store.py` (`245230bb...`) antes de registrar partidos prospectivos con `prospective_tracker.py`.
5. **Agregar pruebas de invariancia metodológica aisladas:** verificar determinismo ante permutación del orden de los 10 JSONs, invariancia del conjunto principal ante tarjetas rojas objetivo y coherencia `WalkForwardEvaluator` vs `CoherentMatchModeler`.
