# Auditoría Semántica de Tarjetas Rojas — Iteración 2.2A

**Archivos modificados:** [`observation_store.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L213-L250) y [`history_quality.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/history_quality.py#L112-L240)

---

## 1. Corrección semántica en `_analyze_red_card_events`

Antes de 2.2A, cuando `events` estaba vacío y `own_red_stat`/`opp_red_stat` eran `None` (`data_availability == "NO_STAT_AND_NO_EVENTS"`), `_analyze_red_card_events` devolvía `distortion_side = "NONE"` y `early_any_red = False`, haciendo indistinguible un partido con ausencia verificada de expulsiones de un partido `SCORE_ONLY_NO_STATS` con información desconocida.

Ahora `_analyze_red_card_events` emite explícitamente:
- **`red_card_status`:**
  - `RED_CARD_CONFIRMED`: si existe al menos un evento de tarjeta roja o estadística `Red Cards > 0`.
  - `NO_RED_CARD_VERIFIED`: si existe `VERIFIED_EVENT_TIMELINE` o `NO_EVENTS_TIMELINE` (estadística `Red Cards == 0`) sin expulsiones.
  - `RED_CARD_UNKNOWN`: si `data_availability == "NO_STAT_AND_NO_EVENTS"` (sin `events` y sin `own_red_stat`/`opp_red_stat`).
- **`red_card_verified`:** `True` para `RED_CARD_CONFIRMED` / `NO_RED_CARD_VERIFIED`, `False` para `RED_CARD_UNKNOWN`.
- **`distortion_side`:** `"UNKNOWN"` cuando `red_card_status == "RED_CARD_UNKNOWN"` (en lugar de `"NONE"`), conservando `"NONE"` únicamente cuando está verificado sin expulsiones.
- **Compatibilidad con snapshots legados:** en `history_quality.py`, si un `HistoricalTeamMatch` legado carece de `red_card_status` en `red_card_detail`, se infiere `"RED_CARD_CONFIRMED"` si `red_card_distortion == True` y `"RED_CARD_UNKNOWN"` en caso contrario, sin fabricar verificaciones.

---

## 2. Sensibilidad del modelo ante `RED_CARD_UNKNOWN` (`test_strict`, `n = 2,319`)

En lugar de eliminar el `99.76%` del corpus (`32,295 / 32,373` filas en `preselection` son `RED_CARD_UNKNOWN`), se evaluó la sensibilidad reduciendo el peso de observaciones `RED_CARD_UNKNOWN` (`unknown_red_weight` en `{1.0, 0.85, 0.70}`):

| Configuración | `unknown_red_weight` | 1X2 RPS | 1X2 Log-Loss | Over 2.5 Brier | BTTS Brier | `mean_home_n_eff` | Sesgo Fav `>=0.55` |
|---|---|---|---|---|---|---|---|
| `M0_plus_Elo` | `1.00` | `0.21648` | `1.01552` | `0.235761` | `0.243788` | `10.582` | `-0.0597` |
| `M0_plus_Elo` | `0.85` | `0.21647` | `1.01551` | `0.235761` | `0.243785` | `10.575` | `-0.0597` |
| `M0_plus_Elo` | `0.70` | `0.21647` | `1.01550` | `0.235761` | `0.243783` | `10.565` | `-0.0597` |
| `M4` | `1.00` | `0.21615` | `1.01442` | `0.237281` | `0.243859` | `10.582` | `-0.0419` |
| `M4` | `0.85` | `0.21614` | `1.01440` | `0.237285` | `0.243857` | `10.575` | `-0.0419` |

**Conclusión:** al ponderar de forma homogénea en `preselection`, `unknown_red_weight` produce variación inferior a `0.00002` en RPS/Log-Loss; su utilidad práctica será en `match_engine` L20 (`6 COMPLETE_DETAILED` verificados + `14 SCORE_ONLY_NO_STATS` desconocidos) para dar ligeramente mayor peso relativo a los partidos con línea temporal verificada cuando coexistan ambos tiers.
