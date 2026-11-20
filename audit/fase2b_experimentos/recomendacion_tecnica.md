# BET ENGINE V4 — FASE 2B.3A
# Recomendación Técnica y Diagnóstico Estructural

**Base empírica:** [`resultados_ab.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/resultados_ab.json) y [`comparativa_modelos.md`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/comparativa_modelos.md)  
**Estado del modelo de producción:** **SIN CAMBIOS** (`CoherentMatchModeler_baseline_2.0_2.1A` permanece congelado; **NO** se emite `CALIBRATED_OUT_OF_SAMPLE`).

---

## 1. Diagnóstico Causal: Qué Provoca Realmente la Compresión y el Sesgo Local

Los experimentos A/B aislados permiten separar causas reales de falsos sospechosos en `CoherentMatchModeler_baseline_2.0_2.1A`:

### 1.1 Causa estructural confirmada del sesgo de localía y dilución de favoritos: mezcla de sede subponderada y sin contracción
- En `B0`, el `75 %` del estimador proviene del agregado `HOME + AWAY` y solo el `25 %` del subconjunto específico de sede (aplicado sin regularización cuando `venue_n >= 3`). Al pasar de `L6` a `L20` en `test_strict`, esta arquitectura diluye la señal local y agrava el sesgo de favoritos `P >= 0.55` (`-0.0291` en `L6`, `-0.0809` en `L10`, `-0.1342` en `L20`).
- **La sede es estructuralmente necesaria:** desactivarla (`B1_venue_blend_0`) empeora severamente `RPS` (`+0.001265` WF; `+0.002600` Test) y destruye la resolución de favoritos (`n = 70 -> 19`, sesgo `-0.1410` WF / `-0.1848` Test).
- **Sin contracción, aumentar peso de sede falla:** subir el peso a `0.50` sin regularizar (`B2_venue_blend_50_unshrunk`) amplifica ruido muestral temprano (`fold1 +0.00212`) y degrada significativamente `O2.5` (`+0.001908`) y `BTTS` (`+0.001943`).
- **Con contracción bayesiana hacia un ancla multiplicativa (`venue_k = 4.0`), la señal emerge limpia:**
  - `B3_venue_shrunk_50_k4` elimina `68 %` del sesgo de victoria local en WF (`-0.0282 -> -0.0091`), casi divide por dos `ECE_hw` (`0.030644 -> 0.017507`), evita el deterioro en totales (`Brier O2.5 -0.000111`, `BTTS -0.000162`), mejora los tres folds WF (`[-0.00065, -0.00005, -0.00055]`), supera el bootstrap semanal (`[-0.000714, -0.000199]`) y reduce fuertemente el error en `test_strict` (`ΔRPS = -0.001072`, IC95 `[-0.001626, -0.000550]`, `Fav bias: -0.1342 -> -0.0834`).
  - `B4_venue_shrunk_70_k4` lleva el sesgo local en WF a prácticamente cero (`+0.0012`, `ECE_hw = 0.007303`) y alcanza `ΔRPS = -0.001777` en `test_strict`, pero empieza a tensionar `Brier O2.5` en WF (`+0.000291`) y a sobrepredecir victoria local en `UNKNOWN` (`+0.0219`) y `DEPTH_10_19` (`+0.0182`), tendencia que se vuelve estadísticamente adversa en `B5_venue_shrunk_80_k4` (`O2.5 +0.000567`).
  - Por disciplina pre-registrada (`train_wf` diario con IC95 que incluye 0: `[-0.001227, +0.000466]` en `B3`), la familia `B3..B5` queda clasificada como **`MEJORA_NO_CONCLUYENTE`** y **no se promueve todavía por los resultados de `test_strict`**.

### 1.2 Contracción y tamaño muestral efectivo (`A6_kish_neff_k6` vs `A3..A5`)
- **Sobre-contracción confirmada como dañina:** subir `shrink_k = 12.0` (`A5`) empeora de forma consistente en WF (`+0.000447`, `[+0.000122, +0.000858]`) y en Test (`+0.000496`, `[+0.000165, +0.000840]`).
- **Por qué `A6_kish_neff_k6` es la única variante `MEJORA_RESPALDADA` en 1X2 — y por qué no basta por sí sola:**
  - Cumple todos los criterios primarios de WF (`ΔRPS = -0.000580`, IC95 `[-0.001264, -0.000044]`, `ΔLL = -0.002023`, 3/3 folds negativos, alt partition `ΔRPS = -0.000654`, y dirección coherente en Test `ΔRPS = -0.000533`).
  - **Precaución matemática crítica:** `A6` combinó `use_kish_neff=True` y `shrink_k=6.0`. Como `(sum w)^2 / sum(w^2)` es invariante a la escala de `w < 1`, elevó `mean_home_n_eff` de `6.878` a **`8.759`**, desactivando parte de la penalización hacia el prior cuando un club acumula partidos fuera de su liga/temporada (`w = 0.25 / 0.50`). Esto agudizó la discriminación `1X2` global, pero **agravó ligeramente el sesgo local (`-0.0282 -> -0.0349` WF; `-0.0553 -> -0.0609` Test), subió `ECE_hw` (`0.0306 -> 0.0361` WF; `0.0562 -> 0.0622` Test), elevó `Brier BTTS` (`+0.000502` WF) y empeoró `RECONSTRUCTED` en Test (`0.22216 -> 0.22360`)**.

### 1.3 Hipótesis descartadas como motores principales
1. **Priors empíricos por estrato o liga (`A1`, `A2`):** `A1` **empeora** (`+0.000397` WF; `+0.000325` Test) al inflar `λ_away` (`+0.0425`), y `A2` oscila entre folds (`MEJORA_NO_CONCLUYENTE`). Los priors fijos `(1.56, 1.26)` no son el problema principal frente al esquema de ponderación por sede.
2. **Exclusión agresiva de amistosos (`C1`, `C2`) o copas (`C3`):** eliminar amistosos al inicio de temporada reduce `n_eff` (`208` partidos caen `< 6` en WF bajo `C2`) y **empeora** `fold1` (`+0.00142` en `C1`), aunque sea levemente favorable una vez consolidada la temporada (`-0.000303` en `kept >= 6`). Restringir copas (`C3`) no se sostiene fuera de WF (`+0.000347` en los `585` afectados de `test_strict`).
3. **Identidad competitiva estricta (`C4`):** necesaria para no equiparar `UNKNOWN == UNKNOWN`, pero con impacto estadístico prácticamente neutro (`-0.000014` WF; `+0.000069` Test).

---

## 2. Decisiones Técnicas Inmediatas (No Promoción)

1. **Mantener congelado `CoherentMatchModeler_baseline_2.0_2.1A`** en [`bet_engine/modeling/distributions.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py). Ni `A6_kish_neff_k6` (por empeorar localía, `ECE_hw`, `BTTS` y `RECONSTRUCTED`) ni `B3/B4` (por IC diario no excluyente de 0 en `train_wf` temprano y límite de diseño de experimento aislado) deben promoverse directamente a producción en esta fase.
2. **Mantener el bloqueo de calibración:** no habilitar `CALIBRATED_OUT_OF_SAMPLE`, no modificar umbrales Shadow/BET y no inferir valor económico (`EV`, `ROI`, `CLV`).
3. **Preservar la higiene del próximo holdout:** dado que `test_strict` (`2,323` partidos) ya se utilizó como pasada confirmatoria única en esta fase, cualquier iteración de diseño nueva **no deberá ajustarse ni seleccionarse sobre `test_strict`**, reservando exclusivamente datos prospectivos reales posteriores a `2026-11-14T19:45:00+00:00` para acreditación final.

---

## 3. Hoja de Ruta Recomendada para Fase 2B.3B

1. **Desacoplar `A6` y probar combinación ortogonal `B3 + A_desacoplado` sobre base `C4`:**
   - Evaluar separadamente: (i) `use_kish_neff=True` con `shrink_k = 8.0` vs `sum(w)` con `shrink_k = 6.0`; (ii) combinar la mejor regulación de contracción con la contracción bayesiana de sede en banda conservadora (`B3`: `venue_blend ~ 0.50–0.60`, `venue_k = 4.0`) para atacar simultáneamente compresión `1X2` y sesgo local sin dañar `O2.5`/`BTTS`; y (iii) activar `strict_competition_identity = True` (`C4`) como estándar de integridad.
2. **Probar ponderación gradual (no eliminación binaria) de amistosos:**
   - En lugar de eliminar amistosos con pérdida de cobertura en agosto (`C1/C2`), probar peso reducido (`0.10–0.25`) o exclusión condicionada a contar con `>= 8–10` partidos oficiales recientes.
3. **Incorporar ajuste explícito por fuerza del rival histórico:**
   - Incluso en `B4`, persiste en `test_strict` un sesgo residual de favoritos `P >= 0.55` (`-0.0653`) y de `bias_hw` (`-0.0287`), consistente con la falta de ajuste por dificultad de calendario/rival (Elo/ajuste de oposición pendiente de etapa 2B.3B).
4. **Acreditación prospectiva con mercado real:**
   - Una vez congelado un único candidato tras validación walk-forward ampliada sin usar futuros, contrastar en el holdout prospectivo (`> 2026-11-14T19:45:00Z`) calibración probabilística multicriterio (`RPS`, `LogLoss`, `O2.5`, `BTTS`, `ECE`) junto a trazabilidad de precios reales Bwin pre-kickoff.
