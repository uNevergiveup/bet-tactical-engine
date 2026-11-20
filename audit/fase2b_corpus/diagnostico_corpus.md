# BET ENGINE V4 — FASE 2B.1: CERTIFICACIÓN DE CORPUS HISTÓRICO PARA BACKTESTING

**Fecha:** 2026-11-19  
**Rama:** `refactor/etapa2-fundamentos`  
**Alcance:** Auditoría y certificación del corpus disponible sin modificar el modelo probabilístico, sin ajustar parámetros y sin ejecutar backtesting comparativo masivo.

---

## 1. Inventario completo y procedencia

El pipeline histórico descubre **10 archivos JSON** (`tools/reproduce_baseline_backtest.py::_discover_source_files`), que contienen **945 filas objetivo (`878` `fixture_id` únicos)** y **23,063 partidos finalizados únicos embebidos** en `statistical_observations.{home,away}` (`1,589` equipos).

| Archivo fuente | SHA-256 (16 hex) | Bytes | Filas | Retenidas | Duplicadas | `generated_at_utc` |
|---|---|---:|---:|---:|---:|---|
| `outputs/reconciliation_real_20261111/coverage_models_final/preselection_cache_recovered.json` | `eda44f051bf5a51f` | 2,299,148 | 64 | 64 | 0 | `2026-11-11T13:04:13.653252+00:00` |
| `outputs/discovery/2026-11-02_1500_0300/prospective_trial/enriched/preselection.json` | `20218d6ef05c5054` | 450,231 | 13 | 13 | 0 | `2026-11-02T20:32:35.469161+00:00` |
| `outputs/discovery/2026-11-03_1500_0300/prospective_trial/enriched/preselection.json` | `ca8327bf94636731` | 121,271 | 3 | 3 | 0 | `2026-11-03T22:35:09.945657+00:00` |
| `outputs/discovery/2026-11-03_1600_0200/prospective_trial/enriched/preselection.json` | `bf6b6658c37f9d9e` | 120,772 | 3 | 2 | 1 | `2026-11-03T21:48:58.325569+00:00` |
| `outputs/discovery/2026-11-07_1500_0300/prospective_trial/enriched/preselection.json` | `f1095a2f3ea9dec8` | 22,877,586 | 637 | 636 | 1 | `2026-11-07T00:51:48.959819+00:00` |
| `outputs/discovery/2026-11-09_1500_0300/prospective_trial/enriched/preselection.json` | `ada40f30a2acb128` | 1,569,699 | 45 | 45 | 0 | `2026-11-09T00:27:58.199511+00:00` |
| `outputs/discovery/2026-11-10_1500_0300/prospective_trial/enriched/preselection.json` | `f44191416d7d1d91` | 2,790,401 | 82 | 82 | 0 | `2026-11-09T23:04:54.068517+00:00` |
| `outputs/discovery/2026-11-11_1500_0300/prospective_trial/enriched/preselection.json` | `122ef053a86adff6` | 2,265,733 | 64 | 0 | 64 | `2026-11-11T13:04:13.653252+00:00` |
| `outputs/discovery/2026-11-12_1500_0300/prospective_trial/enriched/preselection.json` | `9f9cadb94bcd7f97` | 830,857 | 25 | 24 | 1 | `2026-11-12T16:43:39.150710+00:00` |
| `outputs/discovery/2026-11-17_1500_0300/prospective_trial/enriched/preselection.json` | `705a4fe44016403e` | 512,723 | 9 | 9 | 0 | `2026-11-17T13:00:33.117155+00:00` |
| **Total** | — | **33,838,422** | **945** | **878** | **67** | — |

**Distinción crítica de dos capas:**
1. **Capa de filas objetivo (`945` / `878` únicos):** son instantáneas de preselección (814 sin campo `status`, 47 `NS`, 17 con estado `HT`/`1H`/`FT`/`PST`). **Carecen de marcador final consolidado (`goals`) y contienen `0` cuotas Bwin** (`0/945`). No son evaluables directamente como objetivos de backtest, sino que sirven como contenedores fuente.
2. **Capa de partidos finalizados embebidos (`23,063` únicos, `status="FT"`):** provienen de `statistical_observations.{home,away}` (`6,508` observados por ambos equipos y `16,555` por un lado). Todos poseen resultado `goals` (`23,063/23,063`), pero **`0` cuotas históricas Bwin** y **`0` XI objetivo**.
3. **Expedientes individuales complementarios (`outputs/matches/`):** `170` carpetas de evento (`167` con archivos `cuotas_bwin_*.txt`, `13` con `match_engine_*.json`, `8` con `shadow_comparison_*.json`); además de su naturaleza heterogénea, carecen de etiqueta unificada de resultado OOS en este corpus y son insuficientes para calibrar EV/CLV de mercado de forma masiva.

---

## 2. Recuento de partidos aptos, parciales y descartados

### 2.1. Certificación de los `23,063` partidos históricos embebidos (Capa B)

| Estado de certificación | Conteo | % sobre `23,063` | Alcance / Significado |
|---|---:|---:|---|
| **`BACKTEST_ELIGIBLE`** (`GOALS_FT`, `1X2`, `BTTS`, estricto sin roja) | **4,489** | **19.46 %** | Resultado `FT` válido en ambos lados (`4,411` `BOTH_SIDES` + `78` reconstruidos unilateralmente), `red_card_distortion=False`, y `>= 6` partidos limpios previos (`available_at_utc <= t_ko`, `kickoff < t_ko`) por ambos equipos (`3,135` con `>= 10` previos; solo `50` con `>= 20`). |
| **`BACKTEST_PARTIAL_COVERAGE`** (roja objetivo o métricas secundarias) | **7** *(+ subconjunto dentro de 4,489)* | **0.03 %** | `7` partidos cumplen `min6` pero tienen tarjeta roja registrada en el partido objetivo (`EXCLUDED_TARGET_RED_CARD`). Dentro de los `4,489` aptos para goles FT, solo **`133`–`143`** son aptos para `min6` en microdatos secundarios y **`9`** en `GOALS_H1`. Además, `977` de `4,489` (`21.76 %`) caen en `stratum="UNKNOWN"` por omisión de `league_name` en filas históricas. |
| **`BACKTEST_INSUFFICIENT_DATA`** | **18,567** | **80.51 %** | Excluidos por `PRIOR_HISTORY_LT_6` (menos de 6 partidos limpios previos en alguno de los dos equipos dentro del corpus acumulado). |
| **`BACKTEST_TEMPORAL_UNVERIFIED`** | **0** *(con salvedad de provenance)* | **0.00 %** | Secuencia relativa `kickoff < t_ko` consistente en `23,063`. Sin embargo, `available_at_utc` (`kickoff + 125 min`) es **estimado sintéticamente** (`_estimate_available_at_utc`), no marca real de ingesta. |
| **`BACKTEST_IDENTITY_CONFLICT`** | **0** | **0.00 %** | `0` conflictos de marcador, `kickoff`, `team_id` o `league_id` entre las `6,508` observaciones bilaterales (`HOME`/`AWAY`) de los `23,063` fixtures embebidos. |
| **`BACKTEST_DUPLICATE`** *(Capa B / colapsados)* | Colapsados en `23,063` | — | Múltiples referencias al mismo `fixture_id` deduplicadas por `(team_id, fixture_id)` sin discrepancias de valores. |

### 2.2. Certificación de las `945` filas de preselección objetivo (Capa A)

- **`BACKTEST_DUPLICATE`:** **67** filas (`64` solapadas exactamente entre `preselection_cache_recovered.json` y `2026-11-11/preselection.json`; `3` duplicadas adicionales en `2026-11-03_1600`, `2026-11-07`, `2026-11-12`).
- **`BACKTEST_IDENTITY_CONFLICT`:** **66** de las `67` filas duplicadas difieren en campos temporales (`kickoff_utc=None` en `preselection.json` vs informado en `preselection_cache_recovered.json`, además de 3 partidos reprogramados: `1648699`, `1649518`, `1638339`).
- **`BACKTEST_TEMPORAL_UNVERIFIED` / captura tardía:** **17** filas en `preselection_cache_recovered.json` traen estado live/final/pospuesto (`HT=4`, `1H=6`, `FT=4`, `PST=3`) pese a heredar `generated_at_utc = 2026-11-11T13:04:13Z` (y `1` fila tiene `generated_at_utc >= kickoff_utc`).
- **`BACKTEST_INSUFFICIENT_DATA` (como objetivo de backtest):** **878 / 878** (`100 %`) sin marcador final consolidado ni cuotas Bwin vinculadas.

---

## 3. Tabla de exclusiones con causas

| Capa | Causa / Código | Conteo | Clasificación | Motivo técnico y efecto |
|---|---|---:|---|---|
| Capa B (`23,063`) | `PRIOR_HISTORY_LT_6` | **18,567** | `BACKTEST_INSUFFICIENT_DATA` | Al menos uno de los dos equipos no acumula 6 partidos previos limpios antes del `kickoff`. |
| Capa B (`4,496` con `min6`) | `EXCLUDED_TARGET_RED_CARD` | **7** | `BACKTEST_PARTIAL_COVERAGE` | Tarjeta roja registrada en estadísticas del partido objetivo; excluido del split estricto de baseline. |
| Capa B (`4,489` estricto) | `MISSING_MICROSTATS_SECONDARY_LT_6` | **4,346–4,356** | `BACKTEST_PARTIAL_COVERAGE` | Aptos para `GOALS_FT`/`1X2`/`BTTS`, pero sin `min6` previo + dato objetivo en córners/tiros/faltas/amarillas. |
| Capa B (`4,489` estricto) | `MISSING_GOALS_H1_LT_6` | **4,480** | `BACKTEST_PARTIAL_COVERAGE` | Solo `237` objetivos tienen `goals_h1` y apenas `9` cumplen `min6` previo bilateral. |
| Capa B (`4,489` estricto) | `STRATUM_UNKNOWN_OMITTED_LEAGUE_NAME` | **977** | `BACKTEST_PARTIAL_COVERAGE` | Filas históricas con `league_id != target_league_id` sin `league_name` (p. ej. ligas `41`, `42`, `45`, `667`), clasificadas como `"UNKNOWN"`. |
| Capa A (`945`) | `DUPLICATE_TARGET_FIXTURE` | **67** | `BACKTEST_DUPLICATE` | Solape entre `reconciliation_real_20261111` y `2026-11-11` (`64`), más 3 filas repetidas. |
| Capa A (`878`) | `LIVE_OR_POST_KICKOFF_TARGET` | **17** | `BACKTEST_TEMPORAL_UNVERIFIED` | Filas recuperadas tras el inicio (`1H`/`HT`/`FT`/`PST`) con timestamp heredado `13:04:13Z`. |

---

## 4. Riesgos de fuga temporal (distinguiendo fuga confirmada, riesgo potencial e información insuficiente)

| Vector auditado | Diagnóstico | Evidencia concreta en código y datos |
|---|---|---|
| 1. Estadísticas posteriores al partido objetivo | **Sin fuga confirmada; riesgo potencial por timestamp sintético** | `tools/reproduce_baseline_backtest.py:137-146` filtra `available_at_utc <= t_ko` y `kickoff_utc < t_ko`. Sin embargo, `ObservationStore.from_preselection_match` (`line 1051`) calcula `available_at_utc = kickoff + 125 min` sintéticamente (`_estimate_available_at_utc`), no con hora real de ingesta API. |
| 2. Clasificaciones finales empleadas como prepartido | **Sin fuga en el modelo** | `CoherentMatchModeler` y `reproduce_baseline_backtest.py` no consumen tablas `standings`. |
| 3. Entrenadores o plantillas posteriores al kickoff | **Información insuficiente (sin fuga en modelo)** | `coach_id`/`coach_name` solo consta en **36 / 23,063** (`0.16 %`) partidos y `0` alineaciones XI objetivo. El modelo no los utiliza. |
| 4. Ratings Elo o fuerza de rivales recalculados con futuro | **Riesgo potencial si se añade en 2B.2** | El baseline `2.1A` no usa Elo/SoS; en Fase 2B.2 todo rating o SoS deberá calcularse estrictamente as-of `kickoff < t_ko` (como `build_asof_elo_and_team_history`). |
| 5. Cuotas sin marca temporal fiable | **Información ausente (`0/945` y `0/23,063`)** | Ningún registro del corpus backtest tiene cuota Bwin prepartido con captura verificable. Imposible evaluar ROI, EV realizado o CLV. |
| 6. Múltiples snapshots entre train y test | **Fuga confirmada en split ingenuo 2248; corregida en `split_strict`** | `audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json` confirma que `split_2248` dividía por índice cruzando **85 partidos** en el mismo `kickoff_utc = 2026-09-26T14:00:00+00:00`. El split estricto (`2,170` `<= 13:00Z` / `2,319` `>= 14:00Z`) elimina el solape temporal (`max_train < min_test`). Persiste un **sesgo de descubrimiento/supervivencia**: todos los equipos pertenecen a ligas activas capturadas entre el 02 y el 17/11/2026 (y `78` partidos tienen lado reconstruido desde el otro equipo). |
| 7. Ajuste de parámetros usando el conjunto de prueba | **Sin fuga en baseline `2.1A`; restricción obligatoria para 2B.2** | Baseline `2.1A` usa constantes fijas; cualquier estimación empírica en Fase 2B.2 deberá calibrarse exclusivamente sobre train/walk-forward previo. |

**Riesgos adicionales de calidad en el corpus (`BACKTEST_PARTIAL_COVERAGE`):**
- **Sesgo de disponibilidad de microdatos (`has_detailed_stats`):** Solo `~2,422–2,437` de `23,063` (`10.5 %`) tienen córners/tiros/tarjetas (`det=True`), concentrados en ventanas recientes P1–P6 o ligas principales.
- **Subregistro de tarjetas rojas (`red_card_distortion`):** En `preselection.json` (`observation_store.py:1093`), los `20,626` partidos solo-marcador (`89.4 %`) tienen `red = None`, por lo que quedan con `red_card_distortion = False` sin verificación real (`RED_CARD_UNKNOWN`), a diferencia de los `39` partidos con roja detectada dentro del subconjunto `det=True`.
- **Temporada `2027` (`22` partidos) y `UNKNOWN` (`977` partidos):** API-Football etiqueta `season=2027` en torneos estatales/africanos tempranos (`Pernambucano - 1`: 16, `NPFL`: 5, `Elite One`: 1), y la omisión de `league_name` en `statistical_observations` degrada `977` partidos (`41`, `42`, `45 FA Cup`, `667 Friendlies`) a `competition_stratum="UNKNOWN"`.

---

## 5. Cobertura por competición, temporada y familia

### 5.1. Por familia de mercado (sobre los `4,489` partidos limpios con `min6` en goles FT)

| Familia | Objetivo con dato | `min >= 6` previos por bando + objetivo | Aptitud para backtesting Fase 2B.2 |
|---|---:|---:|---|
| **`GOALS_FT` / `1X2` / `BTTS` / `Over-Under`** | **4,489** | **4,489** *(3,135 con `min>=10`, 50 con `min>=20`)* | **APTO** (núcleo principal: `4,411` bilaterales + `78` reconstruidos). |
| **`CORNERS`** | 736 | **143** | **PARCIAL / MUESTRA PEQUEÑA** (solo exploratorio, sesgo de ligas con microdatos). |
| **`SHOTS`** | 727 | **143** | **PARCIAL / MUESTRA PEQUEÑA** |
| **`ON_TARGET`** | 736 | **143** | **PARCIAL / MUESTRA PEQUEÑA** |
| **`FOULS`** | 664 | **134** | **PARCIAL / MUESTRA PEQUEÑA** |
| **`YELLOW_CARDS`** | 731 | **133** | **PARCIAL / MUESTRA PEQUEÑA** |
| **`GOALS_H1`** | 237 | **9** | **NO APTO** (`n=9` insuficiente). |

### 5.2. Por temporada, estrato y ligas principales (sobre `4,489`)

- **Temporadas:** `2026`: **4,160** (`92.67 %`), `2025`: **296** (`6.59 %`), `2027`: **22** (`0.49 %`), `2024`: **11** (`0.25 %`).
- **Estratos (actuales en parser):** `DOMESTIC_LEAGUE`: **3,350** (`74.63 %`), `UNKNOWN`: **977** (`21.76 %`), `NATIONAL_TEAM`: **79** (`1.76 %`), `DOMESTIC_CUP`: **57** (`1.27 %`), `CONTINENTAL_CLUB`: **26** (`0.58 %`).
- **Principales ligas por volumen (`269` ligas únicas):** `253` MLS (`191`), `45` FA Cup (`131`), `51` (`122`), `41` (`115`), `731` (`111`), `42` (`110`), `931` (`107`), `59` (`105`), `58` (`105`), `564` (`104`), `60` (`104`), `50` (`102`).

---

## 6. Protocolo de partición cronológica propuesto

Para evitar toda fuga temporal y respetar el tamaño real (`4,489` partidos limpios desde `2025-10-11` hasta `2026-11-14`):

1. **Partición principal temporal estricta (`split_strict_no_boundary_tie_no_rc`):**
   - **Train / Calibración:** **2,170** partidos (`2025-10-11T18:00:00+00:00` a `2026-09-26T13:00:00+00:00`). Solo este bloque puede utilizarse para estimar priors por estrato/liga, pesos de sede o parámetros Elo/SoS.
   - **Test / Evaluación OOS congelada:** **2,319** partidos (`2026-09-26T14:00:00+00:00` a `2026-11-14T19:45:00+00:00`). Brecha estricta de 1 hora entre el último partido de train y el primero de test (`0` empates de frontera).
2. **Análisis de sensibilidad obligatorios:**
   - **Ventanas `L6`, `L10`, `L20`:** informar métricas por separado sobre `n=2,319` (y explícitamente sobre el subconjunto `BOTH_SIDES` sin reconstrucción unilateral).
   - **Segmentación:** `ALL`, `CLUB` (`DOMESTIC_LEAGUE` puro vs mezcla), `NATIONAL_TEAM`, y análisis separado de los `977` actualmente etiquetados `UNKNOWN` tras resolver su `league_name` (propuesta mínima para 2B.2).
   - **Criterio estricto de métricas y calibración:**
     - **Brier Score** (`1X2` y binarios `home_win`, `draw`, `away_win`, `over_2_5`, `btts_yes`).
     - **Log Loss** (multiclase `1X2` y binarios).
     - **RPS** (`1X2` ordenado).
     - **ECE** (5–10 bins solo cuando `n >= 200` por segmento; reportar `INSUFFICIENT_SAMPLE` por debajo).
     - **Sesgo por localía (`pred_mean - obs_rate`):** medir explícitamente la infravaloración local / sobrevaloración visitante y empate detectada en el baseline.
     - **Incertidumbre:** **paired block bootstrap** por bloque temporal (semana/jornada, `2,000` réplicas, seed `42`) para intervalos de confianza del delta frente al baseline.
   - **Frontera predictiva vs financiera:** Este corpus acredita **calibración probabilística pura**; **no acredita ROI, EV realizado ni CLV** por ausencia de cuotas históricas fiables (`0/4,489`).

---

## 7. Identificación reproducible del baseline congelado

- **Versión del modelo:** `CoherentMatchModeler_baseline_2.0_2.1A`
- **Hashes SHA-256 del código de referencia:**
  - `bet_engine/modeling/distributions.py`: `b85d7ab092217833234f53f0b9d90b56705c115f4b9a373a25e282ef5d5d8276`
  - `tools/reproduce_baseline_backtest.py`: `5b98386ba76d46bf60ebc3425635871c48aee6ca7098db72c9b780d4a7d41038`
  - `bet_engine/modeling/calibration.py`: `280aeec11dab05c2b9ee5d566af9ae6e99d10585cc35e25127c48756aa970097`
- **Parámetros congelados:**
  - `DEFAULT_PRIORS["goals"] = (1.56, 1.26)`; `STRATUM_PRIORS["NATIONAL_TEAM"]["goals"] = (1.50, 1.40)`
  - `shrink_k = 8.0`, `phi_k = 12.0`, `dispersion_floors["goals"] = 1.18`
  - Recencia: `0.35 * L6 + 0.65 * L20` (si `n >= 7`); sede: `0.75 * shrunk + 0.25 * venue_mean` (si `venue_n >= 3`)
  - Pesos de competición: `1.0` (misma liga y temporada), `0.50` (mismo estrato), `0.25` (otro estrato)
  - Combinación ataque/defensa: `0.55 * own + 0.45 * opp`; Dixon–Coles `rho = -0.04`, `max_goals = 12`; rojas: `exclude_any`
- **Baselines comparativos ya disponibles:** en `WalkForwardEvaluator` (`bet_engine/modeling/calibration.py`) figuran `legacy_p6`, Poisson sin contracción (`unshrunk_poisson`) y tasa base (`naive_base_rate`); en `audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json` constan además `train_base_rates_strict`.
- **Métricas de referencia observadas en `audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json` (`split_strict` `L20`, `n_test=2,319`):**
  - `1X2`: `brier_per_outcome = 0.20401`, `log_loss = 1.02074`, `rps = 0.21823`
  - Sesgo observado de localía: `home_win` (`pred_mean = 0.4096` vs `obs_rate = 0.4644`, `-5.48 pp`, `ece_5bins = 0.055714`), `draw` (`0.2524` vs `0.2285`, `+2.39 pp`, `ece_5bins = 0.023855`), `away_win` (`0.3380` vs `0.3070`, `+3.10 pp`, `ece_5bins = 0.045318`)
  - `over_2_5`: `brier = 0.235623`, `log_loss = 0.663679`, `ece_5bins = 0.03795` (`pred_mean = 0.5536` vs `obs_rate = 0.5912`)
  - `btts_yes`: `brier = 0.243869`, `log_loss = 0.680994`, `ece_5bins = 0.014677` (`pred_mean = 0.5800` vs `obs_rate = 0.5666`)

---

## 8. Criterios de aceptación para la Fase 2B.2 (sin implementar cambios todavía)

1. **Invariancia y trazabilidad del corpus:** Toda prueba A/B en 2B.2 debe ejecutarse sobre el mismo conjunto `split_strict_no_boundary_tie_no_rc` (`2,170` train / `2,319` test), excluyendo `reconciliation_real_20261111` duplicado (`64`) y las `17` filas live/tardías si se inspeccionan targets.
2. **Correcciones mínimas recomendadas antes del backtest comparativo (a implementar solo al iniciar 2B.2):**
   - Resolver el mapa `league_id -> (league_name, competition_stratum)` en `ObservationStore.from_preselection_match()` para evitar que `977` partidos válidos caigan a `stratum="UNKNOWN"`.
   - Etiquetar explícitamente `BOTH_SIDES` (`4,411`) vs `RECONSTRUCTED` (`78`) para auditar sesgo de reconstrucción.
3. **Condición de promoción estadística:** Una variante candidata en 2B.2 (p. ej. priors por liga/estrato aprendidos en train, mayor peso de sede, filtro de amistosos o Elo/SoS as-of) solo puede aceptarse si mejora de forma estadísticamente consistente `log_loss`, `brier_score`, `RPS` y reduce el sesgo local/visitante (`pred_mean - obs_rate`) en `split_strict_test` con intervalo bootstrap de bloque sin degradar `over_2_5` ni `btts_yes`.
4. **Separación de acreditación financiera:** Aunque supere el backtesting predictivo en `GOALS_FT`/`1X2`, la acreditación para apostar (`CALIBRATED_OUT_OF_SAMPLE` en `BwinMarketEvaluator`) requerirá además evidencia prospectiva con cuotas Bwin reales capturadas antes del kickoff (`prospective_registry.py`).
