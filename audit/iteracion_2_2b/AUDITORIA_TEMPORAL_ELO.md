# Auditoría Temporal de `AsOfEloTracker` y Disponibilidad — Iteración 2.2B

**Archivos modificados:** [`observation_store.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L133-L144) y [`experimental_v2_1b.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py#L54-L162)  
**Pruebas:** [test_iteracion_2_2b_prospective_and_elo.py](file:///Users/antonio/Projects/bet-tactical-engine/test/test_iteracion_2_2b_prospective_and_elo.py)

---

## 1. Defecto temporal detectado y corregido

En 2.2A, `_estimate_available_at_utc` asignaba `kickoff + 125 min` indistintamente a `FT`, `AET` y `PEN`, lo que permitía que partidos resueltos en prórroga (`AET`, duración real ~155 min) o penaltis (`PEN`, ~170 min) apareciesen como disponibles prematuramente a `+125 min`.

**Corrección implementada (`resolve_elo_available_dt` y `_estimate_available_at_utc`):**
- **`FT`:** cota mínima `kickoff + 125 min`.
- **`AET`:** cota mínima **`kickoff + 165 min`** (evita fuga antes de concluir la prórroga).
- **`PEN`:** cota mínima **`kickoff + 180 min`** (evita fuga antes de concluir la tanda de penaltis).
- **Estado desconocido o falta de timestamp verificado:** fallback conservador **`kickoff + 180 min`**.
- **Disponibilidad explícita observada (`observed_available_at_utc` / `finished_at_utc`):** se toma `max(observed_dt, ko_dt + min_offset_for_status)`, sin permitir adelantar por debajo de la duración mínima física del partido.
- **Partidos no terminados/interrumpidos (`NS`, `1H`, `HT`, `2H`, `ET`, `INT`, `SUSP`, `PST`):** excluidos en `AsOfEloTracker.build_from_fixtures` (`excluded_non_finished`), nunca alteran ratings.
- **Compatibilidad:** todos los fixtures legados `FT` conservan `+125 min` y las ablaciones/experimentos previos de 2.1B/2.2A reproducen exactamente sus resultados.
