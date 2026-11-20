# Diagnóstico de Intervalos de Incertidumbre y PIT — Iteración 2.2B

**Conjunto evaluado (previo al corte de `test_strict`, sin reoptimizar contra `test_strict`):** `val_select` (`n = 1,087`, `2026-09-05` .. `2026-09-26T13:00Z`), evaluado con `M0_plus_Elo`.

---

## 1. Separación de conceptos

1. **Intervalos predictivos de conteos enteros (`[q10, q90]` de goles, nominal `80%`):**
   - Cobertura observada en `val_select` (`n = 1,087`):
     - Goles local (`home`): **`0.9209`**
     - Goles visitante (`away`): **`0.9402`**
     - Goles totales (`total`): **`0.8611`**
   - La cobertura supera el `80%` nominal debido a la discreción natural de los conteos bajos (`0–3` goles).
   - **Randomized PIT en 10 deciles (ideal ~`108.7` por decil):** `[120, 106, 103, 103, 93, 105, 100, 102, 125, 130]`. Presenta una leve forma en **U** (ligero exceso de densidad en colas extremas `0` y `>=4` goles respecto a Poisson-Dixon-Coles puro).

2. **Incertidumbre epistémica sobre probabilidades (`[p_low, p_high]` para `P(home_win)`):**
   - **Método actual en producción (esquinas deterministas `(lh_lo/hi, la_lo/hi)` con `_gamma_rate_bounds`, `z = 1.2816`):**
     - Anchura media en `val_select`: **`0.3916` (`39.2 pp`)**, `raw_bin_coverage = 1.0`.
     - Problema: combinar simultáneamente los extremos opuestos `(lh_lo, la_hi)` y `(lh_hi, la_lo)` asume correlación `-1` perfecta en el error de estimación, inflando artificialmente el ancho de banda y bloqueando `ev_lo > 0`.
   - **Alternativa posterior conjunta Monte Carlo Gamma (`S = 60` muestras independientes `lh ~ Gamma`, `la ~ Gamma`):**
     - Anchura media en `val_select`: **`0.2842` (`28.4 pp`)**, **`-10.74 pp`** más estrecha que las esquinas manteniendo `raw_bin_coverage = 1.0`.
   - **Limitación remanente:** incluso con `28.4 pp` en `P(home_win)`, con `n_eff ~ 10` la incertidumbre paramétrica sobre `lambda` sigue siendo amplia; antes de usar `p_low` para `ev_lo > 0` en producción, debe calibrarse conjuntamente la varianza posterior (`phi`, `n_post`) frente a tasas empíricas fuera de muestra en `val_select` y verificarse en la muestra prospectiva.
