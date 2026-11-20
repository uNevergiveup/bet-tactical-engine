# BET ENGINE V4 — FASE 2B.3B
# Protocolo Congelado de Validación Prospectiva Independiente

**Especificación candidata:** `CoherentMatchModeler_cand_2b3b_v1` (`INT_B3_A6a_kish_k8_C4`)  
**Control de referencia obligatorio:** `CoherentMatchModeler_baseline_2.0_2.1A` (`B0_baseline_frozen`)  
**Estado de acreditación:** **NO ACTIVAR EN PRODUCCIÓN NI SHADOW** hasta superar íntegramente esta evaluación prospectiva.

---

## 1. Elegibilidad y Frontera Temporal Sin Fugas

1. **Partidos estrictamente prospectivos:** únicamente encuentros con `kickoff_utc > 2026-11-14T19:45:00+00:00` no pertenecientes al corpus histórico de `4,496` partidos (ni a `train_strict` ni a `test_strict` `DIAGNOSTIC_INSPECTED`, ni ajustados sobre el evento `7901472`).
2. **Registro previo inmutable (Pre-kickoff lock):** la predicción pareada de **`B0_baseline_frozen`** y **`CoherentMatchModeler_cand_2b3b_v1`** debe calcularse y sellarse antes del inicio del encuentro (`status = NS`, `executed_at_utc < kickoff_utc`), registrando:
   - `fixture_id`, `home_id`, `away_id`, `league_id`, `season`, `stratum` (usando catálogo estable de ligas);
   - `sha256` del snapshot de entrada (`match_engine_<event>.json` o equivalente) y marca temporal real `executed_at_utc`;
   - `min_depth`, `own_home_n`, `min_venue_n` y `n_eff` de cada modelo;
   - Cuotas Bwin pre-kickoff con `odds_captured_utc < kickoff_utc` y `odds_sha256` **solo cuando existan** (sin atribuir valor financiero cuando falte captura verificable).
3. **Liquidación separada post-partido:** los marcadores oficiales (`hg`, `ag`, `status in {FT, AET, PEN}`) se incorporan tras el fin del encuentro en artefacto separado sin modificar las predicciones selladas, garantizando conjunto idéntico de fixtures para ambos modelos.
4. **Cobertura mínima de antecedentes (consistente con pipeline):** partidos con al menos `6` antecedentes limpios disponibles antes del kickoff (`red_card_distortion == False`) en ambos equipos, registrando en tabla aparte cualquier partido excluido o cancelado (`PST`, `CANC`, falta de datos) para evitar sesgo de supervivencia.

---

## 2. Tamaño Muestral Mínimo y Segmentos Predefinidos

Alineado con los umbrales del registro prospectivo en [`MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json):
- **Cohorte primaria `CLUB` (`DOMESTIC_LEAGUE` + `DOMESTIC_CUP` + `CONTINENTAL_CLUB` + club `UNKNOWN`):** mínimo **`N >= 200` partidos** pareados liquidados (recomendable `N >= 300` para estrechar intervalos por bloques diarios) o ventana mínima de `30` días naturales una vez superado `N = 200`.
- **Estrato separado `NATIONAL_TEAM`:** mínimo **`N >= 50` partidos** para emitir veredicto independiente; si `N < 50`, reportar únicamente como descriptivo sin inferencia propia (`EVIDENCIA_INSUFICIENTE`).
- **Regla de no-peeking:** no reoptimizar `shrink_k`, `venue_blend`, `venue_k` ni `rho` tras inspeccionar resultados intermedios; una sola evaluación confirmatoria formal al alcanzar el tamaño muestral objetivo.

---

## 3. Métricas Primarias, Secundarias y Límites de Deterioro Aceptable

Todas las comparaciones se calculan como diferencias pareadas $(\text{Cand} - B_0)$ mediante **block bootstrap diario** (`B = 2,000`, `seed = 42`, comprobando también consistencia semanal y partición por bloques cronológicos).

### 3.1 Criterios obligatorios para considerar `CANDIDATA_VALIDADA` (todos necesarios)
1. **Mejora primaria en puntuación `1X2`:**
   - `ΔRPS 1X2 < 0` con `prob_improves_lt_0 >= 0.975` (IC 95 % diario estrictamente `< 0`) **Y** `ΔLogLoss 1X2 < 0` con `prob_improves_lt_0 >= 0.975`.
   - Consistencia direccional (`ΔRPS < 0`) tanto en el subgrupo `DOMESTIC_LEAGUE` como en la primera y segunda mitad cronológica de la cohorte prospectiva.
2. **Mejora o mantenimiento en calibración y sesgos críticos:**
   - `ECE (5 bins)` de victoria local menor o igual que `B0` + `0.003` (objetivo esperado: reducción sustancial como en WF `0.0306 -> 0.0186`), y reducción de `|bias(1)|` respecto a `B0`.
   - Reducción o mantenimiento de `|bias(favoritos fuertes P >= 0.55)|` (sin inversión severa de signo ni sobre-confianza sistemática `> +0.025`).
3. **Límites estrictos de no-deterioro en mercados secundarios y goles FT:**
   - `Over 2.5 Brier`: `ΔBrier_O25 <= +0.0005` y límite inferior del IC 95 % no significativamente adverso (`prob_improves_lt_0 >= 0.05`, es decir, sin deterioro estadísticamente significativo al 95 %).
   - `BTTS Brier`: `ΔBrier_BTTS <= +0.0005` y sin deterioro significativo al 95 % (evitando explícitamente el fallo detectado en `shrink_k = 6`).
   - `Poisson NLL total` (`GOALS_FT`): `ΔPoisson_NLL <= +0.0020` (esperado `< 0`) y `|Δbias(λ_total)| <= 0.05`.
4. **Control específico de riesgos identificados en 2B.3B:**
   - Reportar explícitamente el subgrupo `INSUFFICIENT_OWN_HOME_LT3` (`own_home < 3`): si la cohorte prospectiva contiene `n >= 40` partidos en este subgrupo y el deterioro local supera `ΔRPS >= +0.0040` o `bias(1) >= +0.040`, documentar la necesidad de condicionar `venue_blend` cuando `own_home < 3` antes de promover.

### 3.2 Regla de rechazo / retorno a baseline
Si no se cumplen conjuntamente los puntos 1, 2 y 3 (o si `ΔO2.5` o `ΔBTTS` muestran empeoramiento significativo al 95 %), se rechaza la promoción de `CoherentMatchModeler_cand_2b3b_v1`, se conserva `CoherentMatchModeler_baseline_2.0_2.1A` como referencia activa y se bloquea cualquier acreditación `CALIBRATED_OUT_OF_SAMPLE`.

---

## 4. Ruta Posterior Condicional

1. **Solo tras pasar la validación prospectiva deportiva (`3.1`):** evaluar calibración económica secundaria únicamente sobre los eventos que dispongan de cuotas reales Bwin con `ODDS_VERIFIED` (`odds_captured_utc < kickoff_utc`), sin atribuir EV/ROI positivo mientras la cobertura verificada de mercado sea insuficiente.
2. **Línea paralela de mejora de datos (Fase 2B.4 / posterior):** completar catálogo de ligas de selecciones nacionales (`UNKNOWN -> CLUB` actualmente en `39` equipos) y timestamps reales de publicación API para poder auditar limpiamente `AsOfEloTracker` como candidata futura independiente, sin mezclarlo con el presente ciclo prospectivo de `CoherentMatchModeler_cand_2b3b_v1`.
