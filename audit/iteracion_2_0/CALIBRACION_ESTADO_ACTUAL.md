# Estado Actual de la Calibración y Acreditación de Familias — Iteración 2.0

**Proyecto:** `bet-tactical-engine`  
**Archivos auditados:** `bet_engine/markets/bwin_evaluator.py`, `bet_engine/modeling/calibration.py`, `bet_engine/orchestration/shadow_pipeline.py`

---

## 1. Mecanismo actual en código (`calibration_accredited_families`)

En producción actual (`HEAD 7390981d8da690e34eb96a1dbf944d9e67546708`), **ninguna familia está acreditada**, aunque la arquitectura del evaluador sí contempla acreditación selectiva:

1. **Definición en `bwin_evaluator.py`:**
   - `_family_calibration_info(family, accredited_families=frozenset())` (`bet_engine/markets/bwin_evaluator.py:124-146`):
     - Si `family in accredited_families`, retorna `(True, "CALIBRATED_OUT_OF_SAMPLE")`.
     - Si `family` pertenece a mercados conjuntos (`GOALS_FT`, `MATCH_RESULT_1X2`, `DOUBLE_CHANCE`, `BTTS`, `DRAW_NO_BET`, `EUROPEAN_HANDICAP_1X2`, `RESULT_AND_BTTS`, `BTTS_AND_EITHER_WIN`, `BTTS_AND_TOTAL_2_5`, `EXACT_SCORE_FT`), retorna `(False, "JOINT_MATCH_CALIBRATION_NOT_ACCREDITED")`.
     - Si `"H1" in family`, retorna `(False, "FIRST_HALF_JOINT_CALIBRATION_NOT_ACCREDITED")`.
     - En otro caso (`CORNERS`, `SHOTS`, `ON_TARGET`, `FOULS`, `YELLOW_CARDS`, etc.), retorna `(False, "LIMITED_PREDICTIVE_EVIDENCE_FAMILY_NOT_CALIBRATED")`.
   - `BwinMarketEvaluator.evaluate_standard_markets(..., calibration_accredited_families: set[str] | frozenset[str] = frozenset())` (`bet_engine/markets/bwin_evaluator.py:346`) usa `frozenset()` por defecto.
2. **Bloqueo en `_classify_option` (`bwin_evaluator.py:242-265`):**
   - Para clasificar una selección como `ACTIONABLE_VALUE` exige simultáneamente:
     1. `lineups_confirmed_complete` (`True`, 22 titulares confirmados).
     2. `calibration_accredited` (`True`, i.e., familia presente en `calibration_accredited_families`).
     3. `ev_lo > 0.0` (ventaja positiva incluso en el extremo inferior `p_low`).
     4. `sample_n >= 10` y ausencia de `SMALL_SAMPLE_EXPLORATORY_ONLY`.
     5. Ausencia de `historical_zero_risk` y `high_tail_sensitivity` (`p_mod >= 0.05`).
   - Cuando falta `calibration_accredited`, añade `CALIBRACION_NO_ACREDITADA:<status>` y degrada a candidato condicional/exploratorio o descartado.
3. **Por qué siempre llega vacío en producción (`ShadowPipelineRunner`):**
   - En `bet_engine/orchestration/shadow_pipeline.py:567-574`, la llamada a `BwinMarketEvaluator.evaluate_standard_markets(bundle, model, odds_map=effective_odds, analytics_result=analytics_res, lineups_confirmed_complete=lineups_confirmed_22, lineup_status=lineup_status)` **no pasa el argumento `calibration_accredited_families`**.
   - Una búsqueda completa sobre `bet_engine/` y `test/` confirma que **`calibration_accredited_families` no se pasa desde ningún punto del pipeline** ni existe cargador o artefacto persistente de acreditación en el repositorio.
4. **Brecha adicional en `WalkForwardEvaluator` (`bet_engine/modeling/calibration.py:616-634`):**
   - Incluso en `WalkForwardEvaluator._summarize`, cuando `oos_n >= MIN_CALIBRATED_N` y se superan Brier, Log-Loss y `ECE < 0.10`, el estado devuelto es `"OUT_OF_SAMPLE_BASELINE_SUPERIOR_PENDING_RESAMPLING_AUDIT"`, nunca `"CALIBRATED_OUT_OF_SAMPLE"`. Tampoco existe todavía un puente formal que emita acreditación por familia tras el remuestreo y auditoría conjunta.

---

## 2. Evaluación empírica actual de calibración por familia (`Iteración 2.0`)

| Familia / Mercado | Muestra OOS disponible en corpus local | Métricas actuales observadas (Baseline L20 / WF) | Diagnóstico de calibrabilidad actual |
|---|---|---|---|
| `GOALS_FT` / `MATCH_RESULT_1X2` / `DOUBLE_CHANCE` / `DRAW_NO_BET` / `BTTS` | **4,496** partidos conjuntos (test = `2,248`; WF interno `n = 4,411`) | `1X2 Brier = 0.20428`, `LogLoss = 1.02191`, `RPS = 0.21811`. `home_win ECE = 0.05568`, `over_2_5 ECE = 0.03633`, `btts_yes ECE = 0.01576`. **Sesgo grave:** `favorite_ge_0_55 = -13.79 pp`, `bias_home = -0.165` goles/partido. | **Acreditable en Iteración 2.1**, *condicionado* a corregir primero compresión de favoritos, factor campo y calibración isotónica/Platt o de temperatura sobre el modelo reconstruido. Actualmente NO debe acreditarse por el sesgo de `-13.79 pp` en favoritos. |
| `GOALS_H1` / `1H_RESULT` / derivados H1 | **`728`** filas únicas equipo-partido (`283` evaluables en WF con `min_prior=3`) | `goals_h1 > 0.5`: `n = 283`, `Brier = 0.24007`, `ECE = 0.04499`, `max_bias = 0.0979`. | **INSUFICIENTE PARA ACREDITACIÓN GENERAL:** muestra conjunta H1 insuficiente (`2.9%` cobertura en `preselection`). Mantener como `FIRST_HALF_JOINT_CALIBRATION_NOT_ACCREDITED` salvo ampliación histórica específica. |
| `CORNERS` | `3,168` filas únicas equipo-partido (`1,747` equipo-individual `min_prior=3`; max 11/equipo; joint pair aún menor) | `corners > 4.5 (w6)`: `Brier = 0.24358`, `ECE = 0.02515` (`w6` peor que `w3`: `delta_brier = +0.00044`). | **NO ACREDITABLE AÚN:** baja densidad por equipo en `preselection` (`10.2%`) y varianza de convolución independiente sin ajuste rival. |
| `SHOTS` / `ON_TARGET` | `3,119` / `3,170` filas únicas (`1,704` / `1,748` con `min_prior=3`) | `shots > 11.5`: `Brier = 0.21276`, `ECE = 0.01519`. `on_target > 4.5`: `Brier = 0.23663`, `ECE = 0.01302`. | **NO ACREDITABLE AÚN:** falta evaluación conjunta local-visitante con ambos equipos detallados (`N_joint` muy reducido frente a `N_team`). |
| `FOULS` | `2,884` filas únicas (`1,611` con `min_prior=3`) | `fouls > 11.5 (w6)`: `Brier = 0.23264`, `ECE = 0.02031`, mejora frente a `w3` (`delta = -0.00354`, `prob_imp = 0.992`). | **CANDIDATO FUTURO** si se captura detalle consistente >=10 partidos + inclusión de árbitro; hoy insuficiente conjuntamente. |
| `YELLOW_CARDS` | `3,175` filas únicas (`1,751` con `min_prior=3`) | `yellow > 1.5 (w6)`: `Brier = 0.22919`, `ECE = 0.02005`, mejora marginal (`prob_imp = 0.548`). | **NO ACREDITABLE AÚN:** sin factor arbitral ni densidad conjunta suficiente. |

---

## 3. Requisitos mínimos propuestos para acreditar una familia (`CALIBRATED_OUT_OF_SAMPLE`)

Ninguna familia debe acreditarse por ajuste de código arbitrario ni por superar un test sintético. La acreditación requiere un artefacto versionado e inmutable generado tras la evaluación Walk-Forward sobre el modelo reconstruido:

1. **Integridad temporal y muestral (sin fuga):**
   - `temporal_leakage_violations == 0` (`available_at_utc <= target.kickoff_utc`).
   - Para familias conjuntas de goles (`GOALS_FT`, `MATCH_RESULT_1X2`, `DOUBLE_CHANCE`, `DRAW_NO_BET`, `BTTS`): `n_oos >= 500` partidos conjuntos reales, `>= 50` equipos y `>= 5` competiciones. (Idealmente separado por estrato `CLUB` y `NATIONAL_TEAM`, exigiendo en `NATIONAL_TEAM` muestra propia o cautela explícita mientras `n_test_nat = 51 < 200`).
2. **Superioridad estadística demostrada:**
   - Mejora significativa (IC 95% bootstrap pareado estrictamente favorable, `prob_alt_improves >= 0.975`) sobre:
     - referencia climatológica estratificada (`simple_base_rate`), y
     - baseline actual (`Iteración 2.0 L20`) en **Brier**, **Log-Loss** y **RPS** (1X2).
3. **Calibración empírica por bandas de probabilidad y cuantiles:**
   - `ECE <= 0.03` en partición por cuantiles y `max |pred - emp| <= 0.05` en las bandas clave (`<0.35`, `0.35–0.55`, `>=0.55`), corrigiendo el sesgo actual de `-13.79 pp` en favoritos.
   - Si se evalúa frente a cuotas de cierre Bwin disponibles (`N_bwin` suficiente, hoy solo `n=6` -> insuficiente), reportar también brecha frente a prob. implícita sin vig y ROI/CLV simulado sin filtrar en muestra.
4. **Calibración de incertidumbre (`[p_lo, p_hi]`):**
   - Sustituir o recalibrar el intervalo de tasas para evitar bandas artificialmente anchas (~35 pp) que invalidan `ev_lo > 0` incluso cuando el estimador tiene señal real; verificar cobertura empírica real al nivel nominal (p. ej. ~80%).

---

## 4. Diseño de almacenamiento y consumo en producción

1. **Artefacto versionado de acreditación:** guardar el resultado auditado en un manifiesto JSON versionado bajo control del proyecto (por ejemplo `config/calibration_accreditation.json` o `outputs/calibration/accreditation_<model_version>.json`) con:
   - `model_version` y `schema_version` / hash de parámetros del modelador;
   - fecha de corte del backtest, rango temporal `train/test`, tamaños `n` por estrato (`CLUB` vs `NATIONAL_TEAM`) y familia;
   - métricas OOS (`brier`, `log_loss`, `rps`, `ece`, `favorite_bias`, `interval_coverage`);
   - lista explícita de `accredited_families` por estrato (ej. `GOALS_FT`, `BTTS`, `MATCH_RESULT_1X2`, `DOUBLE_CHANCE`, `DRAW_NO_BET` solo si aprueban en su estrato y no en `NATIONAL_TEAM` si `n < umbral`).
2. **Consumo en `ShadowPipelineRunner` y `BwinMarketEvaluator`:**
   - `ShadowPipelineRunner.run` (`bet_engine/orchestration/shadow_pipeline.py:567`) debe leer el artefacto cuya `model_version` coincida exactamente con la del `CoherentMatchModeler` ejecutado y pasar `calibration_accredited_families` (filtrado al estrato del partido) a `BwinMarketEvaluator.evaluate_standard_markets`.
   - Si no existe artefacto válido o el `model_version` difiere, el sistema debe degradar automáticamente a `frozenset()` (estado seguro actual: nunca declarar valor accionable sin calibración vigente).
