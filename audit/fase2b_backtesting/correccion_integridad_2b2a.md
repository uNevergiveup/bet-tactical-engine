# BET ENGINE V4 — FASE 2B.2A: CORRECCIÓN DE INTEGRIDAD DEL CORPUS Y PROTOCOLO DE BACKTESTING

**Fecha:** 2026-11-19  
**Rama:** `refactor/etapa2-fundamentos`  
**Alcance:** Corrección de defectos metodológicos confirmados en identidad competitiva, tarjetas rojas PREMATCH, consistencia del evaluador y trazabilidad de manifiestos, sin optimización predictiva ni activación de modelos nuevos en producción.  
**Artefacto de comparación reproducible:** [PROTOCOL_2B2A_COMPARISON.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/PROTOCOL_2B2A_COMPARISON.json)

---

## 1. Errores corregidos y archivos afectados

| Área / Defecto | Diagnóstico previo | Corrección aplicada | Archivo afectado |
|---|---|---|---|
| **Dependencia de orden y pérdida de `league_name` / `competition_stratum`** | En múltiples bundles, `row["league_id"] != target["league_id"]` degradaba observaciones previas a `""` / `"UNKNOWN"` y la carga secuencial sobrescribía `440` claves `(team_id, fixture_id)` (`95` cambios de estrato en `4,496` al invertir orden). | Se añadió [`build_league_catalog_from_preselection`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L341), catálogo opt-in `league_catalog` en [`ObservationStore.from_preselection_match`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L1193) y fusión conmutativa [`prefer_informative_historical_match`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L569); en `reproduce_baseline_backtest._load_corpus` y `WalkForwardEvaluator` se eliminan las `440` sobrescrituras dependientes del orden. | [observation_store.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py), [reproduce_baseline_backtest.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/reproduce_baseline_backtest.py), [calibration.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py) |
| **Identidad falsa ante datos ausentes y `UNKNOWN == UNKNOWN`** | `lid == league_id` aceptaba `0 == 0`; `_estimate_team_rate_detailed` y `_comp_weight` ponderaban `UNKNOWN == UNKNOWN` a `0.50`; `history_quality` y `build_window_summary` equiparaban IDs `<= 0` o estratos `UNKNOWN`. | Se crearon [`same_competition_season`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L319) (`league_id > 0` y `season > 0`) y [`same_meaningful_stratum`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L329) (`!= UNKNOWN/vacío`). Se aplicaron en `ObservationStore` (`build_window_summary`, `_build_all_windows`, `same_target_league`) y `history_quality.py`; en `distributions.py` y `experimental_v2_1b.py` quedaron aisladas bajo `strict_competition_identity: bool = False`. | [observation_store.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py), [history_quality.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/history_quality.py), [distributions.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py), [experimental_v2_1b.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py) |
| **Selección retrospectiva por rojas objetivo (`rc_target`) y semántica de `red=None`** | `WalkForwardEvaluator` (`evaluate_joint_match_walk_forward`, `evaluate_window_depth_and_weighting`, `evaluate_single_team_metric_windows`) excluía partidos objetivo con expulsión posterior al kickoff (`4,496 -> 4,489`). | El parámetro `exclude_target_red_cards` pasa a `False` por defecto (universo PREMATCH principal = `4,496`: `2,173` train / `2,323` test), conservando `exclude_target_red_cards=True` solo como sensibilidad secundaria. Se añaden propiedades explícitas `red_card_status` (`RED_CARD_CONFIRMED`, `NO_RED_CARD_VERIFIED`, `RED_CARD_UNKNOWN`) y `red_card_verified` en `HistoricalTeamMatch`. | [calibration.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py), [observation_store.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py), [reproduce_baseline_backtest.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/reproduce_baseline_backtest.py) |
| **Divergencia `max_goals=10` vs `12` y clipping en `WalkForwardEvaluator`** | `WalkForwardEvaluator` usaba `max_goals=10` (y `[0.001, 0.999]` en `evaluate_window_depth_and_weighting`) mientras producción (`CoherentMatchModeler`) usa `max_goals=12` sin clipping; además `evaluate_joint_match_walk_forward` no comprobaba explícitamente `kickoff_utc < t_ko`. | Alineados por defecto a `max_goals=12` y `clip_probs=False` (con comprobación explícita `kickoff_utc < t_ko` y auditoría `protocol_audit`), manteniendo soporte paramétrico para inspeccionar `max_goals=10`. | [calibration.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py), [reproduce_baseline_backtest.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/reproduce_baseline_backtest.py) |
| **Desfase de hashes en manifiesto `2.2B` (`MANIFEST_CODE_HASH_MISMATCH`)** | Ediciones previas de Fase 2A en `observation_store.py` invalidaron el hash registrado en `audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json`, haciendo fallar 9 casos en las suites `2.2A..2.2C.1` (incluidos los 3 de `2.2C`). | Conservado intacto byte a byte [MANIFEST_MODELOS_CONGELADOS.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json) como referencia histórica y creado el manifiesto versionado [MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json) con trazabilidad completa y hashes reales; actualizados [prospective_tracker.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/prospective_tracker.py) y las suites activas. | [MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json), [prospective_tracker.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/prospective_tracker.py), [test_fase2b2a_corpus_and_protocol_integrity.py](file:///Users/antonio/Projects/bet-tactical-engine/test/test_fase2b2a_corpus_and_protocol_integrity.py) y suites `2.2A..2.2C.1` |

---

## 2. Separación estricta: cambios de protocolo vs cambios en `p_model`

### A. Cambios que afectan únicamente al protocolo de evaluación / integridad de datos (sin alterar el baseline congelado)
- Inclusión de `target` con roja objetivo en el conjunto PREMATCH principal (`exclude_target_red_cards=False`, `4,496` partidos: `2,173` train / `2,323` test) y clasificación explícita de rojas en 3 estados (`RED_CARD_CONFIRMED`, `NO_RED_CARD_VERIFIED`, `RED_CARD_UNKNOWN`).
- Alineación por defecto del evaluador `WalkForwardEvaluator` a `max_goals=12`, `clip_probs=False` y filtro explícito `kickoff_utc < t_ko`.
- Corrección de `same_competition_season` / `same_meaningful_stratum` en metadatos descriptivos (`build_window_summary`, `_build_all_windows`, `history_quality.py`, `same_target_league` en `from_preselection_match`).
- Consolidación conmutativa del corpus (`prefer_informative_historical_match`) y comparación explícita del modo histórico (`legacy_last_wins`) frente al limpio (`clean_order_invariant`).
- Versionado de manifiesto prospectivo `V2_2B2A` conservando intacto el manifiesto original `2.2B`.

### B. Cambios que modifican `p_model` (aislados como variantes identificadas, NO promovidos a producción)
- **Variante identificada `CoherentMatchModeler_comp_identity_2b2a_v1` (`strict_competition_identity=True` + `league_catalog`):**
  - Impide que `"UNKNOWN" == "UNKNOWN"` pondere a `0.50` y que IDs ausentes (`0 == 0`) ponderen a `1.0`, y resuelve los `league_id` históricos mediante catálogo determinista (`263` ligas en corpus + `competition_registry.json`).
  - Por defecto tanto [`CoherentMatchModeler.build_model`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py#L628), [`_estimate_team_rate_detailed`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py#L413) como [`ExperimentalConfig`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py#L44) mantienen **`strict_competition_identity=False`** y `league_catalog=None` para preservar bit a bit el baseline congelado (`CoherentMatchModeler_baseline_2.0_2.1A`) hasta realizar la evaluación A/B reglada.

---

## 3. Recuento actualizado del corpus e integridad competitiva

| Concepto | Valor verificado |
|---|---|
| Archivos fuente / Bundles únicos / Equipos / Fixtures `FT` únicos | `10` / `878` / `1,589` / `23,063` (`6,508` bilaterales) |
| Excluidos por `PRIOR_HISTORY_LT_6` | `18,567` |
| **Universo principal PREMATCH (`exclude_target_red_cards=False`)** | **`4,496`** (**Train `2,173`** / **Test `2,323`**) |
| Análisis secundario retrospectivo sin roja objetivo (`exclude_target_red_cards=True`) | **`4,489`** (Train `2,170` / Test `2,319`; `7` excluidos todos en `NATIONAL_TEAM`) |
| Desglose de rojas en el universo principal (`4,496`) | `RED_CARD_CONFIRMED`: **`7`** (`3` train, `4` test) · `NO_RED_CARD_VERIFIED`: **`32`** (`5` train, `27` test) · `RED_CARD_UNKNOWN`: **`4,457`** (`99.13 %`) |
| Sobrescrituras `(team_id, fixture_id)` de `competition_stratum`/`league_name` prevenidas | **`440`** (`95` cambios de estrato dentro de `4,496` al invertir orden → **`0` con `prefer_informative_historical_match`**) |
| Estratos en `4,496` — Legacy (`last-wins`) | `DOMESTIC_LEAGUE`: `3,348`, `UNKNOWN`: `978`, `NATIONAL_TEAM`: `86`, `DOMESTIC_CUP`: `55`, `CONTINENTAL_CLUB`: `29` |
| Estratos en `4,496` — Orden invariante (`prefer_informative` sin catálogo externo) | `DOMESTIC_LEAGUE`: `3,383`, `UNKNOWN`: `930`, `NATIONAL_TEAM`: `86`, `DOMESTIC_CUP`: `65`, `CONTINENTAL_CLUB`: `32` |
| Estratos en `4,496` — Con catálogo (`build_league_catalog`, `263` ligas) | `DOMESTIC_LEAGUE`: **`3,512`**, `UNKNOWN`: **`656`**, `NATIONAL_TEAM`: `86`, `DOMESTIC_CUP`: **`205`**, `CONTINENTAL_CLUB`: **`37`** |

---

## 4. Comparación de comportamiento antes / después (`L20`, conjunto de prueba cronológico estricto)

| Escenario / Variante | `train_n` / `test_n` | `RPS` | `LogLoss 1X2` | `Brier 1X2` | `P(1)` / `Obs(1)` | `O2.5 Brier` | `NAT TEST n / P(1) vs Obs(1)` |
|---|---:|---:|---:|---:|---|---:|---|
| **1. Baseline congelado `2.1A` secundario (`legacy`, sin `rc_target`)** | 2,170 / 2,319 | `0.21823` | `1.02074` | `0.20401` | `0.4096` / `0.4644` | `0.235623` | `47` / `0.4032` vs `0.3830` |
| **2. Baseline congelado `2.1A` sobre universo PREMATCH (`legacy`, con `rc_target`)** | 2,173 / 2,323 | `0.21824` | `1.02078` | `0.20402` | `0.4095` / `0.4649` | `0.235566` | `51` / `0.4029` vs `0.4118` |
| **3. Protocolo 2B.2A Principal PREMATCH (`order_invariant`, con `rc_target`)** | **2,173 / 2,323** | **`0.21822`** | **`1.02068`** | **`0.20400`** | `0.4096` / `0.4649` | `0.235584` | **`51` / `0.4029` vs `0.4118`** |
| **4. Protocolo 2B.2A Secundario retrospectivo (`order_invariant`, sin `rc_target`)** | 2,170 / 2,319 | `0.21820` | `1.02064` | `0.20399` | `0.4096` / `0.4644` | `0.235641` | `47` / `0.4032` vs `0.3830` |
| **5. Variante aislada `comp_identity_2b2a_v1` (`catalog` + `UNKNOWN != UNKNOWN`)** | 2,173 / 2,323 | `0.21836` | `1.02104` | `0.20410` | `0.4101` / `0.4649` | `0.235592` | `51` / `0.4029` vs `0.4118` |

- **Delta pareado bloque diario (`Variante comp_identity` vs `Protocolo Principal`, `n = 2,323`, `47` bloques):** `ΔRPS = +0.000146` (`IC 95 % [-0.000111, +0.000352]`, `prob < 0 = 0.1175`), `ΔLogLoss = +0.000361` (`IC [-0.000401, +0.000974]`). Confirmación explícita: la corrección honesta de `UNKNOWN != UNKNOWN` + catálogo **no mejora por sí sola el RPS respecto al artefacto de ponderar `UNKNOWN==UNKNOWN`**, por lo que queda correctamente aislada como variante honesta previa a los experimentos A/B de Fase 2B.3 (sin promoverse ni declararse mejora).
- **Auditoría numérica `max_goals=10` vs `max_goals=12` (`n = 2,323`):** `mg10 RPS = 0.21822` vs `mg12 RPS = 0.21822`, `mg10 LogLoss = 1.02068` vs `mg12 LogLoss = 1.02068`; diferencia absoluta máxima en el corpus por partido de **`0.00036512`** en `P(home_win)`, **`0.00022499`** en `P(over_2_5)` y **`0.00004325`** en `P(btts_yes)` (mayor en partidos de `λ >= 3`). Alinear `WalkForwardEvaluator` a `max_goals=12` elimina esta discrepancia estructural frente a `CoherentMatchModeler`.

---

## 5. Estado de los manifiestos y trazabilidad

- **Referencia histórica preservada:** [MANIFEST_MODELOS_CONGELADOS.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json) (`schema_version = "2.2B-frozen-models"`, SHA-256 = `17ba669f7dca3cc5900ce538e24e8ba1c3bbfb6fa52f17c356a3453048dd2f08`) **permanece intacto** conservando `observation_store.py = c9223b468b6ceba0ab92b805fb292766da8165047264bcd6db8eeeb5b5c812d0`, y sigue rechazando código nuevo mediante `MANIFEST_CODE_HASH_MISMATCH`.
- **Nuevo manifiesto versionado activo:** [MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json) (`schema_version = "2B.2A-frozen-models"`, SHA-256 = `9e7eb6806a85361e7ed2a36d6904289751ed6dd32431717190a199d4a7298606`, `supersedes_manifest_sha256 = 17ba669f...`) registra los SHA-256 reales vigentes:
  - `bet_engine/modeling/experimental_v2_1b.py`: `9031179c856b574c777d8ed8ee99c62820f27a9be9b4ee3ab05cefce56ec3be3`
  - `bet_engine/modeling/prospective_registry.py`: `0701287568209ea1341f41193286ff93f66086f14b5a610e9d508fc55572cdbb`
  - `bet_engine/modeling/distributions.py`: `3f690ae1493a30df63fc20301db133af0449935857f82d6b10b3febbf8c68b26`
  - `bet_engine/modeling/calibration.py`: `1868c8c8fbe5b394c41262b849fe9d19268fdf8410f47dc33c0fe18daff3c380`
  - `bet_engine/context/observation_store.py`: `2f0236243e74f160fae604d5e5e2f09257e4c39aa65807fad13dbef4ae143c79`
  - `bet_engine/context/history_quality.py`: `1cc62f8d94f82d70d88f6e733ad6cb398290fe09644545402efc70122bfe77d2`
  - `config/constants.py`: `8bfb38339f628868b53b81f714e3506674fa91a558f76215d228bf7282043325`
  - `tools/reproduce_baseline_backtest.py`: `4429abf8655e9f41950d09943a2392e3975baf029974e10a93319752603cb5a7`

---

## 6. Matriz de pruebas y resultados

| Comprobación / Suite | Resultado | Cobertura verificada |
|---|---|---|
| `test_fase2b2a_corpus_and_protocol_integrity` | **`7/7 OK`** *(cubre las 8 comprobaciones dentro de 49/49 y 11/11)* | Invariancia de orden, `same_competition_season`/`UNKNOWN`, variante aislada sin mutar baseline, no exclusión `rc_target`, semántica 3 estados, `max_goals` 10 vs 12, integridad manifiestos `2.2B` vs `V2`, expedientes cerrados intactos. |
| Suite combinada (`11` módulos: `2.2A`, `2.2B`, `2.2C`, `2.2C.1`, `2.1A`, `2.1B`, `stage2`, `etapa2`, `etapa4`, `p1b1`, `2B.2A`) | **`49/49 OK`** | Incluidos los **3 tests previamente fallidos de `test_iteracion_2_2c_registry_and_elo`** (`test_cli_synthetic_end_to_end`, `test_persistence_hashes...`, `test_result_verification...`) y los equivalentes en `2.2A`, `2.2B`, `2.2C.1`. |
| Suite Shadow / Coach / Analytics (`etapa5_shadow_and_lifecycle`, `coach_regime_integrity_p0`, `decision_classification_audit`, `etapa3_analytics_core`, `2B.2A`) | **`37/37 OK`** | Ausencia de regresiones en Shadow, control de régimen de entrenador, clasificación de `NO BET` y analytics core. |
| `tools/reproduce_baseline_backtest.py --protocol 2b2a` | **Exit 0** | Genera [PROTOCOL_2B2A_COMPARISON.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/PROTOCOL_2B2A_COMPARISON.json); reproduce `frozen RPS=0.21823`, `principal RPS=0.21822`, `overwrites_prevented=440`. |
| Invariancia de orden sobre las `29,571` claves `(team_id, fixture_id)` del corpus real (directo vs inverso) | **`0` diferencias** | Confirmado `0 / 29,571` discrepancias en `(league_name, competition_stratum, season)` tanto con `prefer_informative_historical_match` solo como con `build_league_catalog`. |

---

## 7. Limitaciones restantes

1. **`656` partidos `UNKNOWN` residuales en el corpus (`14.59 %` de `4,496`):** corresponden a ligas históricas cuyos `league_id` (p. ej. `41`, `42`) no figuran como objetivo en los 10 JSON ni de forma unívoca en `competition_registry.json`; su resolución requeriría enriquecimiento offline posterior fuera del alcance de esta fase.
2. **`99.13 %` (`4,457 / 4,496`) `RED_CARD_UNKNOWN` y `100 %` `available_at_utc` sintético:** las instantáneas `preselection.json` carecen de estadística de expulsiones salvo en `39` partidos verificados y estiman `kickoff + 125 min`.
3. **Sin cuotas históricas Bwin ni muestra `L20` representativa (`n = 50`, `62 %` reconstruidos):** no es posible evaluar `ROI` / `EV` / `CLV` ni extrapolar directamente a `L20` completo hasta acumular el holdout prospectivo virgen post-`2026-11-14T19:45:00+00:00` en `prospective_registry.py`.
4. **`test_strict` (`2,323` / `2,319`) ya inspeccionado:** sigue bloqueado para ajuste iterativo de hiperparámetros conforme al protocolo anti-sobreajuste de Fase 2B.2.
