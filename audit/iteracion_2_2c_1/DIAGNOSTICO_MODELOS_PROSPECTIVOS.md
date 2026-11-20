# Diagnóstico de Modelos Prospectivos — Corrección Elo y SoS (Iteración 2.2C.1)

## 1. Defecto reproducido (Iteración 2.2C)
En `bet_engine/modeling/prospective_registry.py` (`record_prospective_prediction`), `elo_tracker` y `team_ordered_by_id` eran parámetros opcionales con valor por defecto `None`, y `tools/prospective_tracker.py` (`record`) no los construía ni los pasaba.

Reproducción sobre `outputs/matches/7905487/inputs/match_engine_7905487.json` (Gales vs Portugal) antes de la corrección:
- `M0`: `lambda_home = 1.3036`, `lambda_away = 1.565`, `P(1) = 0.313423`, `elo_coverage_ok = False`, `sos_coverage_rate = 0.0`
- `M0_plus_Elo`: `lambda_home = 1.3175`, `lambda_away = 1.5928`, `P(1) = 0.311904`, `elo_coverage_ok = False`, `sos_coverage_rate = 0.0` (ajuste Elo **nulo**, `elo_home = 1500.0`, `elo_away = 1500.0`)
- `M4`: `lambda_home = 1.4677`, `lambda_away = 1.5924`, `P(1) = 0.348561`, `elo_coverage_ok = False`, `sos_coverage_rate = 0.0` (SoS **inactivo**)
- `M4_plus_Elo`: `lambda_home = 1.4677`, `lambda_away = 1.5924`, `P(1) = 0.348561`, `elo_coverage_ok = False`, `sos_coverage_rate = 0.0` (**idéntico a M4**)

## 2. Solución implementada
1. **`build_asof_elo_and_team_history` (`bet_engine/modeling/prospective_registry.py`):**
   - Carga el corpus histórico verificado local (`--corpus-root`, vía `_load_corpus`) y las observaciones de `ObservationStore.from_match_engine_json(payload, cutoff_utc=cutoff_dt)`.
   - Filtra estrictamente as-of: `kickoff_utc < cutoff_dt`, `available_at_utc <= cutoff_dt` y `status in {"FT", "AET", "PEN"}`.
   - Construye `AsOfEloTracker` (`k_factor=20.0`, `hfa=55.0`, estratos `CLUB` y `NATIONAL_TEAM` aislados) y `team_ordered_by_id` (historial de rivales para SoS).
2. **`tools/prospective_tracker.py record`:**
   - Pasa `corpus_root=args.corpus_root` (por defecto `.`) a `record_prospective_prediction`.

```python
# bet_engine/modeling/prospective_registry.py (record_prospective_prediction)
if elo_tracker is None or team_ordered_by_id is None:
    auto_elo, auto_team_hist, elo_builder_meta = build_asof_elo_and_team_history(
        bundle=bundle,
        cutoff_dt=cutoff_dt,
        corpus_root=corpus_root,
        extra_fixtures=extra_fixtures,
        extra_team_series=extra_team_series,
    )
    if elo_tracker is None:
        elo_tracker = auto_elo
    if team_ordered_by_id is None:
        team_ordered_by_id = auto_team_hist
```

3. **Trazabilidad y rechazo de comparaciones degradadas:**
   - `pre_kickoff["model_comparison_integrity"]` registra `full_four_model_comparison_valid`, `degradation_reasons`, `elo_effective` y `sos_effective`.
   - `evaluate_prospective_records(..., require_full_four_model_comparison=True)` rechaza registros degradados (`degraded_comparison_rejected_n`) para impedir que variantes degradadas se presenten como comparación completa de 4 modelos.
