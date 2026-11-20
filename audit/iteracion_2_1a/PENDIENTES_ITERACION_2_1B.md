# Pendientes para Iteración 2.1B (Fuera de alcance en 2.1A)

La **Iteración 2.1A** consolidó la reproducibilidad del baseline y amplió la profundidad histórica de goles hasta `20` partidos preservando intactas las fórmulas matemáticas, los controles P0 y los gates operativos. Quedan expresamente pendientes para **Iteración 2.1B**:

1. **Ablación causal y reconstrucción del estimador `lambda` de goles:**
   - Evaluar mediante ablación controlada sobre el baseline reproducible (`train_strict` -> `test_strict`, evitando optimizar iterativamente sobre test) la contribución de cada factor al sesgo demostrado (`favorite_ge_0_55`: `-14.34 pp`; `bias_home`: `-0.1696` goles/partido; `home_win ECE`: `0.0557` en L20):
     - Especificación multiplicativa / log-lineal ataque × defensa frente a `0.55*own + 0.45*opp`.
     - Ajuste de `shrink_k` por estrato y tamaño muestral efectivo.
     - Ponderación as-of por fuerza del rival (Strength of Schedule / Elo sin fuga temporal).
     - Ventaja de campo y decaimiento temporal exponencial (`half_life_days`, régimen de entrenador).
2. **Calibración real de incertidumbre e intervalos (`A4`):**
   - Sustituir o recalibrar la propagación actual por esquinas Wilson-Hilferty (`~33 pp` de ancho medio en L20, `home_win_rate_bounds = 0.3285`), que hoy bloquea artificialmente `ev_lo > 0`.
   - Evaluar PIT discreto y cobertura empírica `[q10, q90]` de goles locales, visitantes y totales, además de calibración condicional de `p_low`.
3. **Acreditación out-of-sample por estrato y familia (`calibration_accredited_families`):**
   - Generar un artefacto versionado de acreditación vinculado a `model_version` y estrato (`CLUB` vs `NATIONAL_TEAM`) y conectarlo en `ShadowPipelineRunner` hacia `BwinMarketEvaluator.evaluate_standard_markets` solo para las familias que superen criterios estadísticos OOS estrictos (`CORNERS`/`SHOTS`/`FOULS`/`YELLOW_CARDS`/`GOALS_H1` siguen sin acreditación hasta contar con muestra conjunta suficiente).
4. **Auditoría de eventos de tarjeta roja en partidos `7..20` (`SCORE_ONLY_NO_STATS`) si el análisis lo requiere:**
   - Decidir tras validar 2.1B si compensa elevar `DEEP_HISTORY_DETAIL_LIMIT` de `6` a `20` (`+~2.7 MB` JSON/partido, sin peticiones HTTP extra) para detectar `early_red_card` también en los partidos `7..20`, o mantener `detail_limit = 6` en producción.
5. **Integración acotada de `AcceptedModelFeature` (`XI`, bajas, entrenador):**
   - Implementar la aplicación real (hoy inerte) de `accepted_features` en `CoherentMatchModeler.build_model` manteniendo el aislamiento epistémico de `exploratory_artifacts`.
