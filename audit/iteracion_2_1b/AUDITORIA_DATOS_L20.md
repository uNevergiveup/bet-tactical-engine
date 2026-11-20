# Auditoría de Integridad y Asimetría de Datos L20 — Iteración 2.1B

**Proyecto:** `bet-tactical-engine`  
**Alcance:** Verificación previa obligatoria sobre reproducibilidad 2.1A, corte temporal as-of, observabilidad de tarjetas rojas en `SCORE_ONLY_NO_STATS` y consistencia L6 vs L20.

---

## 1. Reproducibilidad y control temporal pre-kickoff

1. **Reproducción exacta de Iteración 2.1A:** `tools/reproduce_baseline_backtest.py` regenera de forma determinista exactamente `corpus_summary`, `boundary_audit`, `split_2248_deterministic` y `split_strict_no_boundary_tie_no_rc` de [BASELINE_REPRODUCIBLE.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_1a/BASELINE_REPRODUCIBLE.json).
2. **Cero fuga temporal en `L20` (`preselection` y `match_engine`):** sobre `878` bundles únicos de `preselection` (`32,373` filas equipo-partido), `leak_ko = 0` (`kickoff_utc < cutoff_utc`) y `leak_avail = 0` (`available_at_utc <= cutoff_utc`). En el split `train_strict` / `test_strict` (`2,170` + `2,319` partidos sin rojas objetivo), `train_fids ∩ test_fids == ∅` y `max(train.kickoff_utc) = 2026-09-26T13:00:00+00:00 < 2026-09-26T14:00:00+00:00 = min(test.kickoff_utc)`.

---

## 2. Hallazgo de asimetría en observabilidad de tarjetas rojas (`SCORE_ONLY_NO_STATS`)

La auditoría de [`ObservationStore`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L200-L227) y [`_analyze_red_card_events`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L141-L227) revela un comportamiento importante que debe conocerse y acotarse antes de promover cualquier modelo L20:

1. **En `match_engine_*.json` (`L20 + detail_limit = 6`):**
   - Los `6` partidos más recientes (`COMPLETE_DETAILED`, ej. `11/11` en `match_engine_7903872.json`) disponen de `events` completos y quedan etiquetados como `data_availability = "VERIFIED_EVENT_TIMELINE"`.
   - Los partidos `7..20` (`SCORE_ONLY_NO_STATS`) carecen de `events` y de `statistics["Red Cards"]`, por lo que reciben `data_availability = "NO_STAT_AND_NO_EVENTS"` y `red_card_distortion = False`.
   - **Implicación:** `red_card_distortion = False` mezcla operativamente **"confirmado sin tarjeta roja"** (`VERIFIED_EVENT_TIMELINE`) con **"información de tarjeta roja desconocida"** (`NO_STAT_AND_NO_EVENTS`), rompiendo la simetría de exclusión por roja entre `L6` y `L20` en expedientes `match_engine`.
2. **En el corpus histórico de `preselection` (`878` bundles, `32,373` filas):**
   - `NO_STAT_AND_NO_EVENTS`: **`32,295` filas (`99.76%`)** -> `red_card_distortion = False` por ausencia de detalle.
   - `NO_EVENTS_TIMELINE` (estadística `Red Cards == 0` sin `events`): **`64` filas**.
   - `STAT_COUNT_ONLY_NO_MINUTE` (`Red Cards > 0` sin minuto): **`14` filas** (todas marcadas `red_card_distortion = True`, excluidas en el backtest limpio).
   - Como el `99.76%` de `preselection` es homogéneo en `NO_STAT_AND_NO_EVENTS`, la comparación `L6 vs L10 vs L20` dentro del backtest de `preselection` **no está sesgada** por esta asimetría; sin embargo, al comparar `L6` vs `L20` en producción (`match_engine` con `detail_limit = 6`), los `14` partidos adicionales sí tienen menor observabilidad de rojas que los `6` primeros.

**Recomendación de mitigación (para 2.2):** en producción, si el presupuesto/latencia lo permite (`0` peticiones HTTP extra al estar dentro de `FIXTURE_IDS_BATCH_SIZE = 20`, aunque `+~2.7 MB` de JSON), o bien conservar `detail_limit = 6` pero registrar explícitamente en `ShadowRunReport` la fracción `verified_red_card_coverage` de los `20` partidos.
