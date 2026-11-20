# Propuesta Técnica para Iteración 2.2

1. **Evaluar candidato híbrido `M4_plus_Elo` y `M0_plus_Elo` sobre holdout prospectivo nuevo (> `2026-11-14`):**
   - Combinar la mejora 1X2/favoritos de `M4` (multiplicativo + `venue_blend=0` + `shrink_k=12`) con `AsOfEloTracker` (`elo_weight` en rango `[0.15, 0.25]`), contrastado contra `M0` y `ABL_only_elo_additive` (`0.21648 RPS`, `0.23576 Over 2.5 Brier`, `0.056 ms/partido`), exigiendo mejora conjunta en `1X2` **sin deterioro** en `Over 2.5` ni `BTTS`.
2. **Construcción de Elo persistente as-of separado por estrato (`CLUB` / `NATIONAL_TEAM`):**
   - Mantener aislamiento estricto de grafos (`CLUB` vs `NATIONAL_TEAM`), con degradación explícita de confianza cuando `min(elo_home_n, elo_away_n) < 3`.
3. **Recalibración de intervalos predictivos `[p_low, p_high]`:**
   - Reemplazar las esquinas deterministas por incertidumbre posterior conjunta calibrada (verificando PIT discreto y cobertura nominal `[q10, q90]` del `80%` real).
4. **Manifiesto de acreditación (`calibration_accredited_families`) por estrato y `model_version`:**
   - Conectar en `ShadowPipelineRunner` el manifiesto versionado solo para familias acreditadas en `CLUB` (`GOALS_FT`, `MATCH_RESULT_1X2`, `DOUBLE_CHANCE`, `DRAW_NO_BET`, `BTTS` si superan los umbrales OOS), manteniendo `NATIONAL_TEAM` y mercados de detalle (`CORNERS`, `SHOTS`, `FOULS`, `YELLOW_CARDS`, `GOALS_H1`) en estado conservador hasta reunir muestra suficiente.
5. **Trazabilidad de `verified_red_card_coverage`:**
   - Exponer explícitamente en `ShadowRunReport` cuántos de los `20` partidos históricos tienen `VERIFIED_EVENT_TIMELINE` frente a `NO_STAT_AND_NO_EVENTS`.
