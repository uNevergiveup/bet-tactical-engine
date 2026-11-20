# INVENTARIO TÉCNICO PARA ITERACIÓN 2 — Reconstrucción Probabilística y Calibración

**Nota de alcance:** Este documento inventaría únicamente componentes, parámetros y fuentes **directamente observados** en el repositorio actual tras cerrar P0 (Iteración 1.2). No introduce cambios probabilísticos ni de calibración.

---

## 1. Fuentes históricas y caché local disponibles

1. **Caché API-Football de preselección (`outputs/data/cache/preselect_api/`)**: `8,672` archivos JSON locales.
2. **Caché histórico adicional (`outputs/.api_football_history_cache/`)** y salidas de recuperación (`outputs/history_recovery_v2/`, `outputs/reconciliation_real_20261111/` con `214` JSON, `outputs/prospective_integral_20261111/` con `445` JSON, `outputs/discovery/` con `6,581` JSON).
3. **Expedientes por partido (`outputs/matches/`)**: `158` directorios (excluyendo `_shared`), de los cuales `13` tienen artefactos de evaluación/caso (`match_engine_<eid>.json` actualmente con `deep_history_limit=6` partidos detallados por equipo en `7909713`/`7909714`).
4. **Cobertura declarada vs observada**: `bet_engine/acquisition/coverage.py` (`CoveragePolicyEvaluator`) audita cobertura por métrica (`goals`, `goals_h1`, `corners`, `shots`, `on_target`, `blocked_shots`, `fouls`, `yellow`). En `7909713` (`league_id=10`, Friendlies) `declared_tier="C_LOW"`, pero la cobertura observada de los 6 partidos detallados fue 6/6 (`100%`).

---

## 2. Parámetros probabilísticos actuales en producción (`bet_engine/modeling/distributions.py`)

- **Familias modeladas (`CoherentMatchModeler.FAMILY_MAP`, L598–606):**
  `GOALS_FT -> goals`, `GOALS_H1 -> goals_h1`, `CORNERS -> corners`, `SHOTS -> shots`, `ON_TARGET -> on_target`, `FOULS -> fouls`, `YELLOW_CARDS -> yellow`.
- **Priors planos (`DEFAULT_PRIORS`, L332–341):**
  `goals: (1.56, 1.26)`, `goals_h1: (0.70, 0.51)`, `corners: (5.40, 4.45)`, `shots: (14.80, 12.10)`, `on_target: (5.05, 4.10)`, `fouls: (12.25, 12.40)`, `yellow: (2.00, 2.20)`.
- **Priors por estrato (`STRATUM_PRIORS`, L343–353):**
  Solo existe `"NATIONAL_TEAM"` (`goals: (1.50, 1.40)`, `goals_h1: (0.74, 0.62)`, `corners: (4.95, 4.30)`, `shots: (14.00, 12.80)`, `on_target: (4.60, 4.25)`, `fouls: (12.10, 12.80)`, `yellow: (1.90, 2.15)`).
- **Sobredispersión mínima (`FAMILY_DISPERSION_FLOORS`, L355–363):**
  `goals: 1.18`, `goals_h1: 1.08`, `corners: 1.62`, `shots: 2.40`, `on_target: 1.44`, `fouls: 1.38`, `yellow: 0.95`.
- **Estimación de tasa (`_estimate_team_rate_detailed`, L393–564):**
  - `shrink_k = 8.0` (fijo).
  - `history_limit = 20` (recortado en la práctica a 6 cuando `match_engine_<eid>.json` solo aporta 6 detallados).
  - Pesos por competición: `1.0` misma liga/temporada, `0.50` mismo estrato (`NATIONAL_TEAM`), `0.25` otra competición.
  - Tarjetas rojas: `red_card_policy = "exclude_any"` (peso `0.0` si hay distorsión por roja temprana; existe `"differentiated_weight"` en código sin activarse por defecto).
  - Deduplicación H2H: peso `0.50` al partido compartido en la rama rival.
  - Ventana L6/L20: si `len(pairs) >= 7`, `0.35 * l6_mean + 0.65 * l20_mean`; si `<= 6`, `l20_mean`.
  - Condición local/visitante: si `>= 3` partidos en `own_venue`/`opp_venue`, mezcla `0.75 * shrunk + 0.25 * venue_mean`.
  - Combinación ataque/defensa: `lam = max(0.05, 0.55 * own_shrunk + 0.45 * opp_shrunk)` (aditiva).
  - **Sin ponderación por calidad del rival ni recencia continua (`half_life_days`) en producción.**
- **Modelo conjunto goles FT:** `dixon_coles_joint_grid(lh, la, rho=-0.04, max_goals=12)` (L698).
- **Bandas de incertidumbre:** `_gamma_rate_bounds(lam, n_post, phi, z=1.2816)` (L126, ~IC 80 %) con penalización `0.85` a `n_post` si hay `COMPETITION_MIX_FALLBACK`.

---

## 3. Código experimental ya existente fuera de producción (`bet_engine/modeling/calibration.py`)

- `WalkForwardEvaluator` incluye:
  - Métricas: `brier_score`, `log_loss`, `expected_calibration_error`, `brier_decomposition`, `multiclass_1x2_log_loss`, `ranked_probability_score_1x2`, `paired_bootstrap_delta_ci`, `summarize_probability_calibration`, `evaluate_interval_calibration`.
  - `_estimate_experimental_goal_rate` (L1079) y `evaluate_window_depth_and_weighting` (L1229): variante offline con `half_life_days`, `coach_decay_factor`, `opponent_lookup` + `sos_alpha` (calidad del rival) y combinación `multiplicative` — **no conectada a `CoherentMatchModeler`**.

---

## 4. Criterios de calibración y filtros NO BET (`bet_engine/markets/bwin_evaluator.py`)

- **`_family_calibration_info` (L124–146) y `BwinMarketEvaluator` (L346):**
  `calibration_accredited_families` tiene valor por defecto `frozenset()` y no recibe familias acreditadas desde ninguna ruta productiva -> `calibration_accredited` es siempre `False`.
- **`_classify_option` (L192–280):** para clasificar como `PICK_ACCIONABLE` exige simultáneamente:
  1. `lineups_confirmed_complete == True`
  2. `calibration_accredited == True`
  3. `ev_lo > 0.0` (extremo inferior de banda conservadora positivo)
  4. `sample_n >= 10` y ausencia de `SMALL_SAMPLE_EXPLORATORY_ONLY`
  5. `not historical_zero_risk` (evita Over cuando `leg_n >= 4` y `leg_hit_rate == 0.0`)
  6. `not high_tail_sensitivity` (`p_model >= 0.05`)

---

## 5. Contratos de mercado y equivalencias (`bet_engine/markets/`)

- **`market_equivalence.py`**: normaliza contratos canónicos (`CanonicalContract`), detecta equivalencias entre mercados (p. ej. `FT_1X2_HOME` vs hándicap/doble oportunidad/DNB o líneas equivalentes) y audita precios redundantes (`audit_snapshot_equivalences`).
- **`bwin_evaluator.py`**: evalúa mercados estándar (`GOALS_FT`, `GOALS_H1`, `MATCH_RESULT_1X2`, `DOUBLE_CHANCE`, `BTTS`, `DRAW_NO_BET`, `TEAM_GOALS`, `CORNERS`, `SHOTS`, `ON_TARGET`, `FOULS`, `YELLOW_CARDS`, hándicaps y derivados de resultado) y separa mercados no modelados como `MERCADO_NO_MODELADO` sin inventar probabilidades.
- **Línea de partida para Iteración 2**: ampliar profundidad histórica efectiva (`> 6` partidos cuando existan en caché/API), introducir ponderación por rival/recencia/régimen en el modelo conjunto, calibrar out-of-sample por familia y estrato mediante `WalkForwardEvaluator`, y decidir con datos si/qué familias cumplen criterios cuantitativos para acreditación.
