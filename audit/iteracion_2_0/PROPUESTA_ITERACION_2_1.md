# Propuesta Técnica Priorizada — Iteración 2.1 (Reconstrucción del Motor Probabilístico)

**Proyecto:** `bet-tactical-engine`  
**Base diagnóstica:** `DIAGNOSTICO_PROBABILISTICO.md`, `CALIBRACION_ESTADO_ACTUAL.md`, `BASELINE_BACKTEST.json`, `COBERTURA_HISTORICA.csv`, `SENSIBILIDAD_7909713.json`  
**Principio rector:** NO fabricar picks relajando umbrales (`sample_n >= 10`, `ev_lo > 0`, `lineups_confirmed_complete`, `CALIBRATED_OUT_OF_SAMPLE`). Todo cambio debe demostrarse primero en Walk-Forward as-of (`test >= 2,248` partidos conjuntos) contra `Iteración 2.0 (L20)`.

---

## Prioridad 1 — Desbloquear profundidad histórica real (`L20`) sin romper captura ni P0

- **Problema demostrado:** `config/constants.py:68` define `DEEP_HISTORY_LIMIT = MICRO_HISTORY_LIMIT = 6`, por lo que `generate_context.py` recorta a 6 partidos los snapshots `match_engine_*.json` aunque `ObservationStore` y `CoherentMatchModeler` operan a 20 y `preselection.json` ya recupera 20 partidos (`90.0%` de bundles con `>=10` y `46.5%` con `>=20` en ambos bandos). En backtest OOS, pasar de `L6` a `L20` mejora todas las métricas globales (`1X2 RPS`: `0.22217 -> 0.21811`, `LogLoss`: `1.03336 -> 1.02191`).
- **Acción en Iteración 2.1:**
  1. Elevar `DEEP_HISTORY_LIMIT` por defecto a `20` (o configurable explícitamente por encima de `MICRO_HISTORY_LIMIT = 6`) en `config/constants.py` y `generate_context.py`, manteniendo compatibilidad del campo `legacy_p6` sobre los últimos 6 partidos si se desea preservar el comparador paralelo en `shadow_pipeline.py`.
  2. Permitir que `generate_context.py` reutilice historial válido pre-kickoff de caché local/preselection cuando proceda, validando siempre `available_at_utc <= cutoff_utc` y `status in {"FT","AET","PEN"}`.
  3. Aumentar la captura de estadísticas detalladas en `preselection`/`generate_context` solo donde sea viable por cuota API, priorizando partidos de goles completos (`L20`) para el núcleo 1X2/Totales/BTTS.
- **Impacto esperado y criterio de aceptación:** `sample_n >= 10` alcanzable de forma orgánica (~90% de partidos según `COBERTURA_HISTORICA.csv`), sin tocar el umbral `sample_n >= 10` ni alterar las reglas temporales P0-A/P0-C1/C3/C4.

---

## Prioridad 2 — Reconstruir el estimador de `lambda` (ataque × defensa multiplicativo + SoS/Elo + recencia + localía real)

- **Problemas demostrados:**
  1. `0.55 * own + 0.45 * opp` comprime favoritos (`-13.79 pp` en `p >= 0.55`) y sobrestima underdogs (`+4.56 pp`).
  2. `shrink_k = 8.0` sobre `n <= 6` otorga `61.5%–66.7%` de peso a un prior único por estrato (`(1.50, 1.40)` en selecciones).
  3. No existe ponderación por fuerza del rival en producción, pese a que en el corpus local el `51.5%` de las apariciones de rivales en `L20` ya tienen `>=3` partidos previos as-of (y un Elo/rating secuencial as-of puede cubrir una fracción aún mayor de partidos recientes).
  4. `L20 ALL` subestima goles locales en `-0.1650` goles/partido (`home_win` predicho `0.4096` vs observado `0.4644`).
- **Acción en Iteración 2.1:**
  1. **Especificación multiplicativa / log-lineal (tipo Dixon-Coles / Poisson-Gamma regularizado):**
     $$\lambda_{\text{home}} = \mu_{\text{home, stratum}} \cdot \alpha_{\text{home}}^{\text{att}} \cdot \beta_{\text{away}}^{\text{def}}, \qquad \lambda_{\text{away}} = \mu_{\text{away, stratum}} \cdot \alpha_{\text{away}}^{\text{att}} \cdot \beta_{\text{home}}^{\text{def}}$$
     donde $\mu_{\text{home/away, stratum}}$ incorpora la ventaja real de localía observada en el estrato (ej. `~1.63 vs ~1.35` en clubes, frente al actual `0.75/0.25` venue blend insuficiente).
  2. **Ajuste por fuerza del rival (Strength of Schedule / Elo as-of) + decaimiento exponencial:**
     - Partir de la base experimental ya esbozada en `_estimate_experimental_goal_rate` (`bet_engine/modeling/calibration.py:1079-1205`: `half_life_days`, `coach_decay_factor`, `opponent_lookup`, `sos_alpha`), calibrada estrictamente en la partición de entrenamiento sin fuga.
     - Para `NATIONAL_TEAM` y partidos inter-confederación/inter-categoría: cuando falten rivales en `opponent_lookup`, incorporar ajuste por ranking/rating as-of verificable o aumentar la incertidumbre (`phi`/varianza epistémica) en lugar de empujar a ambos hacia `(1.50, 1.40)`.
  3. **Revisión de `shrink_k` y `rho`:** calibrar `shrink_k` por estrato/familia sobre muestra `L20` de entrenamiento (evitando sobrecontracción cuando `n_eff >= 12`), y reestimar `rho` Dixon-Coles conjuntamente en entrenamiento.
- **Criterio de aceptación OOS (`test = 2,248` mínimo + pruebas sintéticas):**
  - Reducción estadísticamente significativa (`prob_alt_improves >= 0.975`) de `1X2 RPS`, `1X2 Log-Loss` y `Brier` frente a `BASELINE_BACKTEST.json L20` (`RPS < 0.21811`, `LogLoss < 1.02191`).
  - Reducción del sesgo absoluto en `favorite_ge_0_55` de `|-13.79 pp|` a `<= 4.0 pp` y del `bias_home` de `|-0.165|` a `<= 0.05` goles/partido.
  - Mantenimiento estricto de coherencia probabilística (`1X2 = 1.0`, `BTTS`, totales y hándicaps derivados de la misma matriz conjunta).

---

## Prioridad 3 — Calibración empírica, intervalos de incertidumbre realistas y acreditación por estrato/familia

- **Problemas demostrados:**
  1. `calibration_accredited_families` siempre es `frozenset()` en `ShadowPipelineRunner` (`shadow_pipeline.py:567`).
  2. La propagación actual por esquinas de `_gamma_rate_bounds` genera intervalos `[p_lo, p_hi]` de ~`35 pp` de ancho medio, anulando casi cualquier `ev_lo > 0`.
  3. `NATIONAL_TEAM` solo aporta `51` partidos en test, insuficiente para acreditar calibración en ese estrato con igual confianza que `CLUB` (`2,197`).
- **Acción en Iteración 2.1:**
  1. Sustituir el intervalo conservador por esquinas por incertidumbre posterior conjunta coherente (p. ej., muestreo Monte Carlo / cuadratura sobre la posterior Gamma-log-normal de `(\lambda_home, \lambda_away)` usando una varianza posterior empíricamente calibrada al 80% real).
  2. Evaluar si tras la Prioridad 2 el modelo queda bien calibrado nativamente (`ECE <= 0.03`) o requiere capa ligera de recalibración isotónica / temperatura **ajustada exclusivamente en train/validación temporal previa**, nunca en test.
  3. Implementar un **generador de manifiesto de acreditación** desde `calibration.py` y cargarlo en `ShadowPipelineRunner` sujeto a coincidencia exacta de `model_version` y separación por estrato (`CLUB` vs `NATIONAL_TEAM`). Solo familias con OOS acreditado (probablemente `GOALS_FT`, `MATCH_RESULT_1X2`, `DOUBLE_CHANCE`, `DRAW_NO_BET`, `BTTS` en `CLUB` si superan los umbrales) pasan a `CALIBRATED_OUT_OF_SAMPLE`; `CORNERS`, `SHOTS`, `ON_TARGET`, `FOULS`, `YELLOW_CARDS` y `GOALS_H1` permanecen no acreditadas hasta reunir muestra conjunta suficiente.

---

## Prioridad 4 — Integración acotada de `AcceptedModelFeature` (XI, bajas, entrenador) sin contaminar la base estadística

- **Problema demostrado:** `CoherentMatchModeler.build_model` (`distributions.py:595-858`) valida `accepted_features` (`EpistemicContractError` ante artefactos exploratorios), pero **no aplica** ningún ajuste a partir de ellos.
- **Acción en Iteración 2.1:**
  - Mantener intacta la barrera epistémica (`exploratory_artifacts` siempre ignorados, `test_coherence_and_exploratory_isolation_on_real_7903872`).
  - Definir ajustes log-lineales acotados y trazables (por ejemplo cambios de entrenador con decaimiento de historial previo o ausencias acreditadas mediante `AcceptedModelFeature`) **solo** después de que el modelo base P1–P3 demuestre calibración OOS sin ajustes manuales, validando cada feature en ablation study antes de activarla.

---

## Compatibilidad con bloqueos P0 y alcance que NO se toca en 2.1

- Intactos: `P0-A` (identidad temporal Bwin por countdown), `P0-B` (invalidación/resincronización de Shadow tras `refresh-odds`), `P0-C1..C4` (frescura real de cuotas, hashes del `manifest.json`, `_evaluate_closure`, timestamps UTC reales).
- Al cambiar el modelo en Iteración 2.1 debe incrementarse explícitamente `model_version` en `CoherentMatchModeler`, de modo que cualquier snapshot Shadow previo de Iteración 1/2.0 no herede acreditaciones incompatibles ni invalide auditorías históricas en `outputs/matches/`.
- Fuera de alcance en 2.1: relajación de `sample_n >= 10` o `ev_lo > 0`, cambios a `lineups_confirmed_complete`, integración LLM táctica en `p_model`, o alteración de expedientes ya generados en `outputs/matches/`.
