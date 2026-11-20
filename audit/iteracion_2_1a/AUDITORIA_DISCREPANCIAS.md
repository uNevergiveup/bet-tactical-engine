# Auditoría de Discrepancias Numéricas y Reproducibilidad del Baseline — Iteración 2.1A (Fase A)

**Proyecto:** `bet-tactical-engine`  
**Rama:** `refactor/etapa2-fundamentos`  
**Script reproducible:** [reproduce_baseline_backtest.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/reproduce_baseline_backtest.py)  
**Artefacto regenerado:** [BASELINE_REPRODUCIBLE.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json) y [PREDICCIONES_OOS.csv](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_1a/PREDICCIONES_OOS.csv)

---

## 1. Resolución exacta de las 4 discrepancias señaladas (A1)

La auditoría de [DIAGNOSTICO_PROBABILISTICO.md](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_0/DIAGNOSTICO_PROBABILISTICO.md) frente a [BASELINE_BACKTEST.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_0/BASELINE_BACKTEST.json) y [SENSIBILIDAD_7909713.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_0/SENSIBILIDAD_7909713.json) confirma que **los valores correctos en producción eran los de los JSON**, y que el Markdown introdujo errores de transcripción/reconstrucción aproximada tras la compactación de contexto (`step=549`):

| Métrica / Escenario | Valor erróneo en `DIAGNOSTICO_PROBABILISTICO.md` | Valor en artefacto JSON 2.0 | Valor recalculado exacto con el código actual | Causa exacta de la discrepancia |
|---|---|---|---|---|
| **L6 Over 2.5 Brier** (`n = 2,248` ALL) | `0.23925` (`ECE = 0.04328`) | `0.23799` (`ECE = 0.03662`, `BASELINE_BACKTEST.json:152-154`) | `0.237703` (orden `(ko, fid)`) / `0.237914` (orden estable de inserción); `0.237140` en split estricto `n = 2,319` | El Markdown transcribió una cifra errónea no presente en el JSON (`BASELINE_BACKTEST.json` contenía `0.23799` en `L6 ALL` y `0.23787` en `L6 CLUB`). La diferencia residual de `±0.00029` al regenerar `2,248` se explica en la Sección 2 (empate de 85 fixtures en la frontera). |
| **L10 Over 2.5 Brier** (`n = 2,248` ALL) | `0.23714` (`ECE = 0.03526`) | `0.23627` (`ECE = 0.03417`, `BASELINE_BACKTEST.json:781-783`) | `0.236070` (orden `(ko, fid)`) / `0.236326` (orden estable); `0.235526` en split estricto `n = 2,319` | Error de transcripción en `DIAGNOSTICO_PROBABILISTICO.md` frente al valor real de `BASELINE_BACKTEST.json:781` (`0.23627` en `ALL`, `0.23617` en `CLUB`). |
| **`7909713` `shrink_k = 4`** | `lh = 2.3694, la = 0.9030, P(1) = 0.7037` | `lh = 2.3396, la = 0.9425, P(1) = 0.6821` (`SENSIBILIDAD_7909713.json:86-94`) | **`lh = 2.3396, la = 0.9425, P(1) = 0.6821`** (reproducido exactamente llamando a `_estimate_team_rate_detailed(..., shrink_k=4.0)`) | `SENSIBILIDAD_7909713.json` llamó directamente al estimador real de `distributions.py` (incluyendo el `venue_blend = 0.25` sobre `HOME`/`AWAY`); el Markdown anotó valores de una aproximación manual anterior que difería del JSON final. |
| **`7909713` peso uniforme de competición** | `same_stratum_weight_1_0`: `lh = 2.2121, la = 1.0014, P(1) = 0.6462` | `comp_weight_uniform_1_0`: `lh = 2.1214, la = 1.0915, P(1) = 0.6057` (`SENSIBILIDAD_7909713.json:126-134`) | **`lh = 2.1214, la = 1.0915, P(1) = 0.6057`** (verificado con ponderación `1.0` en los 6 partidos de cada bando y `venue_blend = 0.25`) | Con peso uniforme `1.0`, los 2 partidos del Mundial `league_id=1` de Argentina (`0-1` y `2-1`) pasan de peso `0.5` a `1.0` (bajando la media ofensiva de `3.20` a `2.833`) y los 2 de Costa Rica en `league_id=10` (`0-2` y `0-3`) pasan de `0.5` a `1.0`, reduciendo `P(1)` de `0.6372` a `0.6057` (no subiéndola a `0.6462`). |

---

## 2. Auditoría de corpus, reconstrucción de caras y frontera temporal (A2)

Al reconstruir el backtest desde las **10** fuentes locales verificadas (con SHA-256 registrados en `BASELINE_REPRODUCIBLE.json`):
1. **Universo base:** `878` bundles únicos de `preselection`, `1,589` equipos únicos, **`23,063`** fixtures terminados únicos (`FT`).
2. **Diferencia entre `4,496`, `4,489` y `4,411`:**
   - Solo `6,508` de los `23,063` fixtures aparecen explícitamente con fila propia en ambos bandos (`BOTH_SIDES`). En `WalkForwardEvaluator.evaluate_joint_match_walk_forward` (`calibration.py:930+`) se exigen ambas filas explícitas y se filtran tarjetas rojas objetivo, lo que da **`n = 4,411`**.
   - `BASELINE_BACKTEST.json` reconstruyó el marcador del rival ausente desde `opponent_id` + `conceded_metrics["goals"]` sobre los `23,063` fixtures únicos (`4,418` `BOTH_SIDES` + `46` `RECONSTRUCTED_FROM_HOME` + `32` `RECONSTRUCTED_FROM_AWAY` = **`4,496`**, excluyendo exactamente `18,567` por `PRIOR_HISTORY_LT_6`, de modo que `4,496 + 18,567 = 23,063`).
   - De esos `4,496`, **`7`** fixtures objetivo tienen `red_card_distortion = True` en el partido evaluado; al excluirlos siguiendo el protocolo limpio se obtiene **`4,489`** fixtures elegibles.
3. **Empate de `85` fixtures en la frontera temporal (`2026-09-26T14:00:00+00:00`):**
   - Cortar mecánicamente por la mitad (`idx = 2,248`) divide un bloque de **`85` partidos** que arrancan exactamente a `2026-09-26T14:00:00+00:00` (`35` quedaron en train y `50` en test).
   - Con orden determinista `(kickoff_utc, fixture_id)` se obtiene `L6 RPS = 0.22233` (`Over 2.5 Brier = 0.237703`), `L10 RPS = 0.21968` (`0.236070`), `L20 RPS = 0.21825` (`0.236173`), frente a `0.22217 / 0.23799`, `0.21950 / 0.23627`, `0.21811 / 0.23645` en `BASELINE_BACKTEST.json` (que dependía del orden de inserción en memoria del diccionario de fixtures antes de ordenar por `kickoff_utc`).
   - **Solución adoptada en A2 (`split_strict_no_boundary_tie_no_rc`):** mover todo el bloque `kickoff_utc >= 2026-09-26T14:00:00+00:00` al conjunto test y excluir los `7` objetivo con tarjeta roja, logrando **partición temporal estricta**:
     - `train_strict`: `n = 2,170` (`2025-10-11T18:00:00+00:00` .. `2026-09-26T13:00:00+00:00`).
     - `test_strict`: `n = 2,319` (`2026-09-26T14:00:00+00:00` .. `2026-11-14T19:45:00+00:00`), `train_fids ∩ test_fids == ∅`, `max(train.kickoff) < min(test.kickoff)`.

---

## 3. Comando reproducible exacto

```bash
PYTHONPATH=. .venv/bin/python tools/reproduce_baseline_backtest.py \
  --out-json audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json \
  --out-csv audit/iteracion_2_1a/PREDICCIONES_OOS.csv
```

Genera [BASELINE_REPRODUCIBLE.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json) (con SHA-256 de las 10 fuentes, parámetros exactos, auditoría de frontera y splits `2248` y `strict`) y [PREDICCIONES_OOS.csv](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_1a/PREDICCIONES_OOS.csv) (`6,969` filas de predicción = `2,323` fixtures de la unión `test_2248 ∪ test_strict` × `3` ventanas `L6/L10/L20`).
