# Protocolo Prospectivo PREMATCH Implementado — Iteración 2.2B

**Módulo:** [prospective_registry.py](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/prospective_registry.py)  
**CLI ejecutable:** [prospective_tracker.py](file:///Users/antonio/Projects/bet-tactical-engine/tools/prospective_tracker.py)  
**Manifiesto congelado:** [MANIFEST_MODELOS_CONGELADOS.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json)

---

## 1. Garantías implementadas

- **Congelación estricta pre-kickoff:** `record_prospective_prediction` evalúa simultáneamente los `4` modelos congelados (`M0`, `M0_plus_Elo`, `M4`, `M4_plus_Elo`) con entradas idénticas, registra `kickoff_utc`, `kickoff_madrid`, `cutoff_utc`, `generated_utc`, `input_sha256`, `odds_sha256`, `manifest_sha256`, `lineup_status` (distinguiendo `CONFIRMED_COMPLETE_22`, `CONFIRMED_PARTIAL_11`, `PROBABLE_ONLY`, `UNAVAILABLE`), cuotas Bwin y `pre_kickoff_sha256`.
- **Bloqueo anti-retrospectivo:** rechaza (`ProspectiveRegistryError`) si `now_utc >= kickoff_utc`, `cutoff_utc >= kickoff_utc`, `status` es en vivo/terminado, ya existen goles finales o `kickoff_utc <= 2026-11-14T19:45:00+00:00`. Impide sobrescribir registros ya emitidos (`IMMUTABLE_RECORD_ALREADY_EXISTS`).
- **Liquidación post-partido inmutable:** `settle_prospective_record` verifica `pre_kickoff_sha256` íntegro y `settled_at_utc >= resolve_elo_available_dt(kickoff, status)` antes de calcular `rps`, `ll_1x2`, `brier_1x2_per_out`, `brier_o25`, `brier_btts` y errores de goles.
- **Criterio muestral fijado por adelantado:** `CLUB >= 200` partidos liquidados; `NATIONAL_TEAM >= 50` (siempre evaluados por separado; llegar a 30 días no acredita selecciones sin alcanzar `N >= 50`).

---

## 2. Comandos exactos de uso

```bash
# 1) Iniciar registro prospectivo ANTES del kickoff de un partido:
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py record \
  --match-engine outputs/matches/<EVENT_ID>/inputs/match_engine_<EVENT_ID>.json \
  --bwin-txt outputs/matches/<EVENT_ID>/inputs/cuotas_bwin_<EVENT_ID>.txt \
  --manifest audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json \
  --records-dir outputs/prospective_records

# 2) Incorporar resultado oficial verificado DESPUÉS del partido (FT / AET / PEN):
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py settle \
  --record outputs/prospective_records/<FIXTURE_ID>_<EVENT_ID>.json \
  --hg 2 --ag 1 --status FT

# 3) Evaluar comparativa pareada M0 / M0+Elo / M4 / M4+Elo sobre los liquidados:
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py evaluate \
  --records-dir outputs/prospective_records
```
