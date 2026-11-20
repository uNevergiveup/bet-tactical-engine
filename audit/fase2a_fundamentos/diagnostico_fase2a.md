# BET ENGINE V4 — FASE 2A: DIAGNÓSTICO DE FUNDAMENTOS ESTADÍSTICOS

**Fecha:** 2026-11-19  
**Rama:** `refactor/etapa2-fundamentos`  
**Caso de referencia auditado:** Remo vs Chapecoense-sc (Bwin `7901472`, API fixture `1492456`, kickoff `2026-11-18T23:00:00+00:00`)  
**Alcance:** Diagnóstico puro sin modificación de código, probabilidades, umbrales ni expedientes cerrados.

---

## 1. Inventario de componentes y dependencias

| Capa | Archivo | Componentes / Funciones clave | Rol en Shadow |
|---|---|---|---|
| Ingesta y corte temporal | `bet_engine/context/observation_store.py` | `ObservationStore.from_match_engine_json()`, `_resolve_coach_regime()`, `_analyze_red_card_events()`, `_classify_stratum()` | Construye `MatchObservationBundle`, filtra `kickoff_utc < cut_dt`, deduplica `fixture_id`, clasifica tarjetas rojas tempranas y estrato competitivo, y genera `ContextualEvidence` (descanso, entrenador, árbitro, lesiones). |
| Calidad e historial | `bet_engine/context/history_quality.py`, `bet_engine/context/lineup_manager.py` | `evaluate_fixture_lineup_state()`, políticas de cobertura | Audita los `22` titulares (`LINEUPS_API_CONFIRMED`, `primary_official_verified=false`) y profundidad `L6`/`L10`/`L20`. |
| Analítica exploratoria | `bet_engine/analytics/investigator.py`, `bet_engine/analytics/plugins.py` | `TacticalInvestigator`, `sos_adjusted_v1044`, `multi_window_matchup` | Genera `prematch_patterns`, `internal_mechanisms`, `descriptive_splits`, `tactical_hypotheses` y `tactical_dossier_<EID>.json`. **No modifica `p_model`**. |
| Modelado probabilístico | `bet_engine/modeling/distributions.py` | `CoherentMatchModeler.build_model()`, `_estimate_team_rate_detailed()`, `_resolve_prior()`, `dixon_coles_joint_grid()`, `_gamma_rate_bounds()`, `build_count_pmf()` | Estima `lambda_home` / `lambda_away` con Empirical Bayes (`L6`/`L20` + venue + prior) para 7 familias y deriva `1X2`, `Doble Oportunidad`, `BTTS` y líneas `.5`. |
| Evaluación de mercados | `bet_engine/markets/bwin_evaluator.py`, `bet_engine/markets/market_equivalence.py` | `BwinMarketEvaluator.evaluate_standard_markets()`, `_classify_option()`, `classify_option_formal_decision()` | Cruza `MatchProbabilisticModel` con `odds_map`, calcula `EV` central/conservador (`p_low`), deduplica equivalencias y clasifica en 5 estados formales. |
| Orquestación Shadow | `bet_engine/orchestration/shadow_pipeline.py`, `bet_engine/orchestration/adapters.py` | `ShadowPipelineRunner.run_shadow_on_bundle()`, `run_shadow_for_event_dir()` | Coordina guardia PREMATCH, cobertura, analítica, modelado, evaluación Bwin, comparación vs legacy `P-1..P-6` y emisión de `shadow_summary` / `shadow_comparison`. |
| Calibración y registro | `bet_engine/modeling/calibration.py`, `bet_engine/modeling/prospective_registry.py`, `tools/reproduce_baseline_backtest.py` | `WalkForwardEvaluator`, `record_prospective_prediction()`, `settle_prospective_record()` | Backtesting cronológico fuera de muestra (goles FT/H1, `1X2`, `BTTS`) y registro prospectivo as-of. |

---

## 2. Reconstrucción numérica verificable del evento `7901472`

Reconstrucción ejecutada en solo lectura sobre `outputs/matches/7901472/context_updates/db56988fc1b71627b65cfe07/match_engine_7901472.json` (`ObservationStore.from_match_engine_json` + `CoherentMatchModeler.build_model`, `MODEL_VERSION = CoherentMatchModeler_baseline_2.0_2.1A`):

### 2.1. Datos utilizados y pesos aplicados (`GOALS_FT`)

- **Remo (`home_id=1198`, `n=20`):** 18 partidos Série A (`league_id=71`, peso `1.0`) + 2 partidos Copa do Brasil (`league_id=73`, `DOMESTIC_CUP`, peso `0.25`) → `w_sum = 18.50`. 6 recientes con microdatos (`det=True`, P1–P6 bajo Thiago Carpini) y 14 solo marcador (`det=False`). `red_card_distortion = False` en los 20.
- **Chapecoense-sc (`away_id=132`, `n=20`):** 17 partidos Série A (`league_id=71`, peso `1.0`) + 2 Copa do Brasil (`league_id=73`, peso `0.25`) + 1 amistoso (`league_id=667 Friendlies Clubs`, `strat=UNKNOWN`, peso `0.25`) → `w_sum = 17.75`. 6 recientes con microdatos (bajo Rafael Lacerda) y 14 solo marcador. `red_card_distortion = False` en los 20.
- **Prior de estrato (`_resolve_prior`, `target_stratum = "DOMESTIC_LEAGUE"`):** `h_prior = 1.56` (local), `a_prior = 1.26` (visitante), con `shrink_k = 8.0`.
- **Mezcla `L6` / `L20` y sede (`HOME`/`AWAY`):**
  1. `sample_est = 0.35 * l6_mean + 0.65 * l20_mean` (sin decaimiento exponencial por fecha; solo escalón discreto L6 vs L20).
  2. `shrunk = (w_sum * sample_est + 8.0 * prior_mean) / (w_sum + 8.0)`.
  3. Mezcla de sede (`len >= 3`): `final = 0.75 * shrunk + 0.25 * venue_mean` (donde `venue_mean` usa **sin contracción** todos los partidos en esa sede dentro de los 20, ponderados por `comp_weight`).
  4. Combinación ataque/defensa: `lambda = 0.55 * own_final + 0.45 * opp_final`.

### 2.2. Traza aritmética exacta de tasas (`1.5303` y `1.1636`)

| Término | `lambda_home` (Remo) | `lambda_away` (Chapecoense) |
|---|---|---|
| Prior (`STRATUM_PRIORS["DOMESTIC_LEAGUE"]`, `k=8`) | `1.56` | `1.26` |
| Ataque propio (`own`): `w_sum` / `l20` / `l6` / `sample_est` | `18.50` / `0.9730` / `0.8333` / `0.9241` | `17.75` / `0.9155` / `0.6667` / `0.8284` |
| Ataque propio tras contracción EB (`shrunk`) | `1.1161` | `0.9625` |
| Ataque en sede (`venue_n=11`, sin contracción) | `1.2683` (`HOME`) | `0.7632` (`AWAY`) |
| `own_final = 0.75 * shrunk + 0.25 * venue_mean` | `1.1541` | `0.9127` |
| Defensa rival (`opp`): `w_sum` / `l20` / `l6` / `sample_est` | `17.75` / `2.1268` / `2.1667` / `2.1407` | `18.50` / `1.6351` / `1.6667` / `1.6462` |
| Defensa rival tras contracción EB (`shrunk`, mismo prior `1.56` / `1.26`) | `1.9603` | `1.5296` |
| Defensa rival en sede (`venue_n=11`, sin contracción) | `2.0789` (Chape `AWAY`) | `1.2927` (Remo `HOME`) |
| `opp_final = 0.75 * shrunk + 0.25 * venue_mean` | `1.9900` | `1.4704` |
| **Tasa pre-grilla (`0.55 * own + 0.45 * opp`)** | **`1.530254`** | **`1.163626`** |

### 2.3. Transformación a `1X2` (`dixon_coles_joint_grid`)

- Se construye grilla `13x13` (`max_goals=12`) con `dixon_coles_joint_grid(1.530254, 1.163626, rho=-0.04, max_goals=12)` (`rho` fijo `-0.04`, sin estimación por liga ni por partido).
- Resultado exacto tras normalización de grilla:
  - `exp_h = 1.530254` (**`1.5303`**), `exp_a = 1.163626` (**`1.1636`**), `lambda_total = 2.6939`.
  - `home_win = 0.452535` (**`45.25 %`**), `draw = 0.263126` (**`26.31 %`**), `away_win = 0.284339` (**`28.43 %`**).
  - Bandas de incertidumbre Gamma (`z = 1.2816`, intervalo ~80 %): `lambda_home ∈ [1.1588, 1.9760]`, `lambda_away ∈ [0.8075, 1.6157]`, generando `draw ∈ [0.2298, 0.3057]` y `away_win ∈ [0.1640, 0.4308]`.

---

## 3. Matriz de variables: fuente → transformación → modelo → decisión

| Variable / Contexto | Fuente | Campo / Estructura | Módulo consumidor | ¿Modifica `p_model`? | ¿Solo advertencia? | Riesgo / Estado observado en `7901472` |
|---|---|---|---|---|---|---|
| Goles históricos (`L6`/`L20`, sede, copa) | API-Football (`_history_deep`) | `HistoricalTeamMatch.own_metrics["goals"]` / `conceded_metrics` | `ObservationStore` → `distributions.py` | **Sí** (100 % de `lambda`) | Warning `COMPETITION_MIX_FALLBACK` | Mezcla copa/amistoso (`0.25`) y prior `1.56`/`1.26` fijo; sobreestima `lambda_away` (`1.1636` vs `0.7632` real fuera). |
| Clasificación / puntos / necesidad | API-Football `standings` | `ctx["standings"]` (leído en `quick_context` / contexto competitivo) | `TacticalInvestigator` (solo descriptivo) | **No** | No | No entra en `CoherentMatchModeler`; no infla artificialmente a Remo. |
| Entrenadores y cambio reciente | API `coachs` + `lineups` históricos | `ContextualEvidence(category="COACH_REGIME")` | `ObservationStore._resolve_coach_regime()` → `TacticalInvestigator` | **No** | **Defecto:** en `7901472` etiquetó `Lacerda Rafael` como `CURRENT_VERIFIED` (`CONFIRMADO_OFICIAL`) | **Contradicción grave:** `target_fixture_full.lineups` tenía `coach=null` y el informe cualitativo acreditaba a **Vinícius Bergantin** (debut), pero Shadow no leyó ese cambio ni redujo la muestra válida del nuevo régimen (`n=0`). |
| XI confirmados (`22` titulares) | API `fixtures/lineups` (`22:30:58Z`) | `target_lineup_evaluation` | `lineup_manager` → `bwin_evaluator` → `shadow_pipeline` | **No** | Gate operativo obligatorio (`PICK_ACCIONABLE`) | Acreditó `LINEUPS_API_CONFIRMED` (`primary_official_verified=false`). No ajusta tasas por ausencias de titulares. |
| Roles y estadísticas de jugadores | API `players` en `_history_deep` | `ranking_probabilistico`, `patrones_jugadores` | Motor descriptivo legacy | **No** | No (en Shadow) | Shadow no modela props de jugadores (`MERCADO_NO_MODELADO`). |
| Bajas / lesiones (`injuries`) | API `injuries` (`results=0` en `7901472`) | `ContextualEvidence(INJURY_ABSENCE)` | `ObservationStore` | **No** | Omitido si `results=0` | Ausencia de cobertura API ≠ cero bajas; bajas reales (`Matheus Felipe`, preservados de Chape) no alteran `p_model`. |
| Descanso y congestión | API `kickoff` + `_next_5` | `ContextualEvidence(SCHEDULE_FATIGUE)` | `ObservationStore` → `TacticalInvestigator` | **No** | Patrón descriptivo | Remo `312.5 h` (`13.02 d`) vs Chapecoense `95.5 h` (`3.98 d`) solo informativo. |
| Fuerza de rivales (SoS / Elo) | `sos_adjusted_v1044` (analítica) / `prospective_registry` (Elo) | `plugin_telemetry`, `skipped_plugins` | `TacticalInvestigator` (solo analítica) | **No** | Skipped en `7901472` (`NO_PRIOR_OPPONENT_REFERENCE_IN_POOL`) | **Cero ajuste SoS en `p_model`:** el `1-6` de Remo en campo de Palmeiras pesa igual que ante un rival descendido. |
| Hipótesis tácticas cualitativas | `reporte_cualitativo_<EID>.md` | `exploratory_artifacts` | `CoherentMatchModeler` (contabilizados e ignorados) | **No** (`ignored_exploratory=18`) | Auditoría Codex | Blindaje epistémico correcto: ninguna hipótesis no calibrada contamina `p_model`. |

---

## 4. Errores confirmados (con archivo, función y evidencia)

1. **[P0 — Defecto de software / Integridad contextual] Etiquetado erróneo de entrenador vigente como `CURRENT_VERIFIED` / `CONFIRMADO_OFICIAL` ante debutante (`coach=null` en objetivo)**  
   - **Archivo y función:** `bet_engine/context/observation_store.py` → `_resolve_coach_regime()` y `from_match_engine_json()` (líneas `696-806`).  
   - **Evidencia:** En `outputs/matches/7901472/shadow/tactical_dossier_7901472.json`, `ctx_1492456_away_coach_regime` declaró `current_coach_name="Lacerda Rafael"`, `coach_status="CURRENT_VERIFIED"`, `verification_status="CONFIRMADO_OFICIAL"` y `regime_change_detected=false`, pese a que `fixtures/lineups` del partido objetivo devolvió `coach={"id": 0, "name": null}` y `reporte_cualitativo_7901472.md` documentó la contratación de **Vinícius Bergantin** (`13/11/2026`, `n=0`).

2. **[P1 — Limitación estadística severa / Sesgo de especificación] Sobrevaloración sistemática del visitante débil (`1.1636` vs `0.7632` real fuera) por combinación de prior alto, peso insuficiente de sede (`25 %`) y mezcla de goles recibidos fuera del local**  
   - **Archivo y función:** `bet_engine/modeling/distributions.py` → `STRATUM_PRIORS`, `_estimate_team_rate_detailed()` (líneas `488-528`).  
   - **Evidencia verificada en `7901472`:**  
     - `STRATUM_PRIORS["DOMESTIC_LEAGUE"]["goals"] = (1.56, 1.26)` (total `2.82` goles/partido) con `shrink_k = 8.0` empuja la tasa ofensiva de Chapecoense de `0.8284` (`sample_est`) a `0.9625` (`+16.2 %`).  
     - El promedio por sede (`venue_mean`) solo pondera un `25 %` (`0.75 * shrunk + 0.25 * venue_mean`, líneas `522` y `526`), carece de contracción EB propia y de ponderación por recencia. Así, el `1-6` recibido por Remo **fuera** ante Palmeiras (`fid=1492447`) eleva la concesión esperada de Remo en casa a `1.4704` (frente a `1.2927` real en casa). Resultado neto: `lambda_away = 1.1636` (frente a `0.7632` de Chapecoense fuera y `0.65` en sus 17 salidas de Série A), elevando `P(away_win)` a `28.43 %` frente al `~15.4 %` implícito de Bwin (`6.50`).

3. **[P1 — Defecto de software / Limpieza de muestra] Mezcla de amistosos (`Friendlies Clubs`) y copas cuando existe muestra suficiente en liga (`>= 17` partidos)**  
   - **Archivo y función:** `bet_engine/modeling/distributions.py` → `_estimate_team_rate_detailed()` (`comp_weight`, líneas `461-466`).  
   - **Evidencia:** Chapecoense tenía 17 partidos recientes de Série A en `L20`, pero `comp_weight` asignó peso `0.25` a `fid=1641093` (`Friendlies Clubs`, `strat=UNKNOWN`) y a 2 partidos de Copa do Brasil, contaminando innecesariamente las tasas de liga (`COMPETITION_MIX_FALLBACK`).

4. **[P1 — Defecto de software / Arquitectura de calibración] Desconexión estructural entre `WalkForwardEvaluator` y la acreditación en runtime**  
   - **Archivo y función:** `bet_engine/modeling/calibration.py` (`_summarize`, líneas `616-634`) y `bet_engine/orchestration/shadow_pipeline.py` (`run_shadow_on_bundle`, línea `631`).  
   - **Evidencia:** `WalkForwardEvaluator` nunca emite `"CALIBRATED_OUT_OF_SAMPLE"` (se detiene en `"OUT_OF_SAMPLE_BASELINE_SUPERIOR_PENDING_RESAMPLING_AUDIT"`), y no existe registro persistido que alimente `calibration_accredited_families` en `adapters.py`. (La corrección previa ya habilitó el parámetro en `ShadowPipelineRunner` para pruebas controladas, manteniendo `frozenset()` en producción.)

5. **[P2 — Limitación estadística] Ausencia de ajuste por fuerza del rival (SoS / Elo) en `CoherentMatchModeler`**  
   - **Archivo y función:** `bet_engine/modeling/distributions.py` (`CoherentMatchModeler.build_model`) y `bet_engine/analytics/plugins.py` (`sos_adjusted_v1044`).  
   - **Evidencia:** En `7901472`, `sos_adjusted_v1044` produjo `0` patrones (`NO_PRIOR_OPPONENT_REFERENCE_IN_POOL` para los 40 partidos) y `CoherentMatchModeler` no ajusta goles por calidad del oponente (tratando por igual un `1-6` ante el líder Palmeiras que un partido ante un colista).

6. **[P2 — Limitación estadística] Parámetro `rho = -0.04` fijo en Dixon–Coles y tratamiento asimétrico de varianza `GOALS_FT` vs resto**  
   - **Archivo y función:** `bet_engine/modeling/distributions.py` (líneas `214`, `702`).  
   - **Evidencia:** `rho=-0.04` está hardcodeado sin estimación por competición; además, en `prob_over` para `GOALS_FT` las bandas usan el mismo `rho=-0.04` con límites Gamma independientes para `lh` y `la`.

---

## 5. Riesgos no demostrados (diferenciados de errores confirmados)

- **Fuga temporal en `ObservationStore`:** **No se confirmó fuga.** `parse_side()` (líneas `597-600`) rechaza explícitamente partidos con `available_at_utc > cut_dt` o `kickoff_utc >= cut_dt` y excluye el `fixture_id` objetivo (`line 585`).
- **Duplicación de partidos o doble conteo H2H:** **No se confirmó error.** `seen_ids` evita duplicados por equipo (línea `588`), `apply_h2h_discount=True` aplica factor `0.50` a H2H compartidos (línea `484`), y la varianza se calcula sobre `unique_obs` por `fixture_id` (línea `531`).
- **Sesgo por disponibilidad de estadísticas (`has_detailed_stats`):** **Riesgo acotado para `GOALS_FT`, pero real para métricas secundarias.** Para goles, los `14` partidos sin microdatos (`det=False`) sí aportan `goals` (score-only), por lo que `GOALS_FT` usa `n=20`; en cambio, córners, tiros, tarjetas y faltas solo disponen de `n=6` (`det=True`), quedando sujetos a mayor varianza y posible sesgo de cobertura de API-Football en ligas menores.
- **Manipulación de `p_model` por hipótesis cualitativas:** **Descartado.** Los 18 artefactos exploratorios de `7901472` quedaron registrados en `ignored_exploratory_in_p_model=18` sin tocar `p_model`.

---

## 6. Capacidad real de backtesting existente

- **Corpus histórico disponible en disco:** `tools/reproduce_baseline_backtest.py` descubre `10` archivos (`preselection_cache_recovered.json` + `9` `outputs/discovery/*/prospective_trial/enriched/preselection.json`) con **`945` filas / `878` `fixture_id` únicos**.
- **Qué existe ya implementado en `WalkForwardEvaluator` (`bet_engine/modeling/calibration.py`) y `tools/reproduce_baseline_backtest.py`:**
  - Evaluación walk-forward estrictamente cronológica (`available_at_utc <= kickoff_utc`) sin fuga temporal.
  - Métricas implementadas: Brier Score (con descomposición), Log-Loss binario y multiclase `1X2`, RPS (`ranked_probability_score_1x2`), ECE (`expected_calibration_error`), MAE, cobertura de intervalos `80 %` (`evaluate_interval_calibration`) e intervalos bootstrap por bloques (`paired_bootstrap_delta_ci`).
  - Baselines implementados: `legacy P-1..P-6`, Poisson sin contracción y tasa climatológica naive.
  - Corpus auditado en `audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json` (`split_strict_no_boundary_tie_no_rc`).
- **Qué falta para backtesting completo y acreditación de apuestas:**
  1. **Cuotas históricas vinculadas al corpus:** Las `945` filas de `preselection.json` tienen **`0` referencias a cuotas Bwin con hora de captura**. Solo existen `116` archivos `outputs/matches/*/inputs/cuotas_bwin_*.txt` (con `Captura:`) y `8` con `shadow_comparison_*.json` — insuficientes aún para calibrar EV/CLV en backtesting masivo de precios sin ampliar la recolección prospectiva (`prospective_registry.py`).
  2. **Familias secundarias (`CORNERS`, `YELLOW_CARDS`, `SHOTS`, `ON_TARGET`, `FOULS`, `GOALS_H1`):** La cobertura histórica en `preselection.json` con estadísticas detalladas completas es limitada frente a `GOALS_FT`, y `WalkForwardEvaluator` no acredita todavía ninguna familia secundaria.
  3. **Criterio formal de promoción a `"CALIBRATED_OUT_OF_SAMPLE"`:** Falta definir el umbral exacto de auditoría de remuestreo/bootstrap y de calibración por estrato/familia para emitir un registro versionado de familias acreditadas.

---

## 7. Propuesta mínima y ordenada de correcciones (para Fase 2B)

1. **[P0] Conciliación de cambio de entrenador / régimen (`observation_store.py`):** No promover a `CURRENT_VERIFIED` / `CONFIRMADO_OFICIAL` un entrenador previo cuando `target_fixture_full.lineups` traiga `coach=null` o cuando `quick_context` / informe cualitativo reporte nuevo técnico (`coach_status="UNKNOWN_OR_NEW_REGIME"`), marcando explícitamente `regime_change_suspected` en el dosier Shadow.
2. **[P1] Ajuste empírico de priors por competición/estrato, mayor peso de sede y exclusión de amistosos (`distributions.py`):**
   - Calibrar `STRATUM_PRIORS` (o derivar prior empírico por liga cuando `n` de liga es suficiente) en lugar de `1.56 / 1.26` fijo para toda liga doméstica.
   - Aumentar el peso de la muestra en la sede correspondiente (`HOME`/`AWAY`) con contracción EB propia hacia el promedio general del equipo, evitando que goleadas recibidas fuera (p. ej. `1-6` en Palmeiras) distorsionen la concesión en casa.
   - Excluir amistosos (`Friendlies`) y reducir/anular mezcla de copa cuando el equipo tenga `>= 10` partidos válidos en la liga objetivo.
3. **[P2] Incorporar ajuste acotado por fuerza del rival (Elo / SoS as-of) y validar en `WalkForwardEvaluator`:** Evaluar en Fase 2B si un ajuste SoS as-of reduce Brier, Log-Loss y ECE fuera de muestra en `GOALS_FT` y `1X2` frente al baseline `2.1A` antes de promover cualquier cambio.
4. **[P2] Protocolo de acreditación OOS (`calibration.py` + `prospective_registry.py`):** Definir la transición explícita desde `OUT_OF_SAMPLE_BASELINE_SUPERIOR_PENDING_RESAMPLING_AUDIT` a `CALIBRATED_OUT_OF_SAMPLE` tras superar bootstrap + calibración de precios en registro prospectivo.

---

## 8. Criterios de aceptación para iniciar la Fase 2B

1. **Integridad de regresión verificada:** Las 45 pruebas focalizadas (`test_decision_classification_audit`, `test_p1a_controlled_corrections`, `test_probabilistic_audit_stage2`, `test_market_coverage_and_taxonomy_stage1`, `test_lineup_management_p0`, `test_corrective_audit_v10`, `test_multi_slot_orchestrator_v3`, `test_operational_case_integration_v3`) pasan `45/45` en `1.275 s` sin alterar ningún artefacto cerrado de `7901472`.
2. **Aislamiento de cambios:** Toda modificación propuesta en Fase 2B debe compararse A/B contra el baseline congelado `CoherentMatchModeler_baseline_2.0_2.1A` sobre `split_strict_no_boundary_tie_no_rc` (sin fuga temporal ni optimización sobre `7901472`).
3. **Criterio estadístico de mejora:** Ninguna corrección de priors, peso de sede o SoS pasa a producción si empeora `log_loss`, `brier_score`, `RPS` o `ECE` fuera de muestra respecto al baseline `2.1A`.
