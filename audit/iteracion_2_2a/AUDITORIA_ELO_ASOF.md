# Auditoría Técnica de `AsOfEloTracker` — Iteración 2.2A

**Código auditado:** [`AsOfEloTracker`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py#L48-L106) y [`predict_experimental_match`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py#L256-L370)  
**Pruebas:** [test_iteracion_2_2a_elo_and_red_cards.py](file:///Users/antonio/Projects/bet-tactical-engine/test/test_iteracion_2_2a_elo_and_red_cards.py)

---

## 1. Parámetros y criterio de actualización (congelados sin tunear sobre test)

| Parámetro | Valor congelado | Significado |
|---|---|---|
| `initial_rating` | `1500.0` | Rating base inicial independiente por `(stratum_pool, team_id)` |
| `k_factor` | `20.0` | Sensibilidad de actualización tras cada partido terminado |
| `hfa` (actualización) | `55.0` | Ventaja local usada en expectativa Elo `exp_h = 1 / (1 + 10^(-((rh + hfa) - ra) / 400))` |
| `scale` | `400.0` | Escala logística estándar Elo |
| `mov` | `log1p(|hg - ag|)` si `gd > 0`, si no `1.0` | Factor de margen de goles acotado logarítmicamente |
| `min_matches_for_use` | `3` | Mínimo de partidos previos as-of en ambos bandos para activar ajuste |
| `elo_weight` | `0.25` | Peso log-lineal en predicción: `mult = exp(0.25 * conf * clip((elo_h - elo_a)/400, -1.5, 1.5))`, con `conf = min(1.0, min(elo_hn, elo_an)/10.0)` |

---

## 2. Verificación de invariantes temporales y de diseño

1. **Sin resultado del partido objetivo antes de predecir:** `build_from_fixtures` ordena por `(avail_dt, ko_dt, fixture_id)` donde `avail_dt = kickoff_utc + 125 min` para `FT`. `get_rating_asof(stratum, team_id, target_ko_dt)` exige por búsqueda binaria `entries[mid][0] <= target_ko_dt`. Como el partido objetivo tiene `avail_dt > target_ko_dt`, jamás entra antes de su propia predicción.
2. **Mismo `kickoff_utc` (`t_same`):** dos partidos simultáneos que arrancan en `t_same` tienen ambos `avail_dt = t_same + 125 min > t_same`, por lo que ambos se predicen con el rating previo a `t_same` antes de que ninguno actualice el Elo (demostrado en `test_elo_same_kickoff_target_leak_dedup_and_fallback`).
3. **Deduplicación de fixtures (defecto corregido en 2.2A):** en 2.1B `build_from_fixtures` no deduplicaba `fixture_id` si el llamador pasaba duplicados; en 2.2A se añadió `seen_fids: set[int]` para ignorar cualquier `fixture_id` repetido.
4. **Separación clubes vs selecciones:** la clave del tracker es `(stratum_pool, team_id)` con `stratum_pool in {"CLUB", "NATIONAL_TEAM"}`, impidiendo contaminación entre clubes y selecciones nacionales.
5. **Competiciones desconectadas dentro de `CLUB`:** todas las ligas de clubes comparten el pool `"CLUB"` arrancando en `1500.0`. Ligas domésticas desconectadas no se comparan entre sí salvo que existan partidos puente (p. ej. torneos continentales), pero si un equipo asciende/juega por primera vez contra otra liga su rating `1500.0` inicial asume paridad media base; esto queda documentado como limitación para ligas heterogéneas.
6. **Fallback explícito sin historial:** cuando `elo_hn < 3` o `elo_an < 3`, `get_rating_asof` devuelve `(1500.0, n < 3)`, `elo_coverage_ok = False` y no se aplica multiplicador Elo.
7. **Por qué la cobertura Elo en `test_strict` (`n = 2,319`) es del `100%`:** porque el criterio de elegibilidad del backtest exige `len(hp) >= 6` y `len(ap) >= 6` partidos previos `available_at_utc <= t_ko` para ambos equipos; dado que esos mismos partidos previos actualizan el `AsOfEloTracker`, todo fixture elegible cumple automáticamente `elo_hn >= 6 >= 3` y `elo_an >= 6 >= 3` **sin usar ningún dato futuro**.
