# Cobertura Real L20 y Correspondencia con Shadow — Iteración 2.1A (Fase B)

**Proyecto:** `bet-tactical-engine`  
**Configuración aplicada:** `MICRO_HISTORY_LIMIT = 6` ([constants.py:67](file:///Users/antonio/Projects/bet-tactical-engine/config/constants.py#L67)), `DEEP_HISTORY_LIMIT = 20` ([constants.py:68](file:///Users/antonio/Projects/bet-tactical-engine/config/constants.py#L68)), `DEEP_HISTORY_DETAIL_LIMIT = 6` ([constants.py:69](file:///Users/antonio/Projects/bet-tactical-engine/config/constants.py#L69)).

---

## 1. Arquitectura de separación L6 vs L20 (`B1`, `B2`)

1. **Microcontexto cualitativo (`MICRO_HISTORY_LIMIT = 6`) intacto:** [`MicroHistoryFormatter.format_team_history`](file:///Users/antonio/Projects/bet-tactical-engine/generate_context.py#L3191) continúa recortando el informe TXT a los últimos `6` partidos (`P-1..P-6`) e informando cuántos partidos adicionales existen en el JSON histórico. Ni el parser legacy (`motor_probabilistico.py`), ni los validadores, ni los prompts cualitativos sufren expansión ni cambios de formato.
2. **Historial profundo (`DEEP_HISTORY_LIMIT = 20`) + detalle selectivo (`DEEP_HISTORY_DETAIL_LIMIT = 6`):**
   - Los `6` partidos más recientes (`COMPLETE_DETAILED`) pasan por `/fixtures?ids=...` conservando `statistics`, `events` (tarjetas rojas) y `lineups` (entrenador). Así `CORNERS`, `SHOTS`, `ON_TARGET`, `FOULS`, `YELLOW_CARDS` y el microcontexto mantienen sus 6 registros detallados sin peticiones innecesarias.
   - Los hasta `14` partidos adicionales (`7..20`) se preservan directamente desde `/fixtures?team={id}&last=20` con etiqueta `_history_detail_tier = "SCORE_ONLY_NO_STATS"`.
   - En [`ObservationStore.from_match_engine_json`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L502-L666), cada observación conserva `fixture_id` único, `league_id`, `season`, `competition_stratum`, `kickoff_utc`, `available_at_utc` (`<= cutoff_utc` y `kickoff < cutoff_utc`), `status in {"FT","AET","PEN"}`, `venue` (`HOME`/`AWAY`), `goals` FT y `goals_h1` HT, `provenance={"source": "match_engine_json", "sha256": source_sha}`, y `_analyze_red_card_events` registra explícitamente `NO_STAT_AND_NO_EVENTS` en los partidos `SCORE_ONLY_NO_STATS`.

---

## 2. Cuántos partidos históricos llegan realmente a Shadow antes vs después del cambio

Medición verificada sobre datos locales reales (sin llamadas externas ni modificación de `outputs/matches/`):

| Fuente / Caso evaluado | Llegaba con L6 | Llega con L20 (`SCORE_ONLY` en `7..20`) | Notas de correspondencia real `preselection` <-> caché <-> expediente |
|---|---|---|---|
| **Cachés reales `fixtures?team=&last=20` en `outputs/data/cache/`** (`n = 123` equipos reales) | tope 6 | Mediana **`20`** (`116/123 = 94.3%` con `>= 10` FT; `95/123 = 77.2%` con `20/20` FT) | En equipos de ligas activas llega casi siempre `>= 10` (`94.3%`); el `5.7%` restante corresponde a parones/selecciones con menor muestra reciente o filtros pre-cutoff. |
| **`7903872` (Osasuna vs Athletic Club)** (`outputs/matches/` vs caché `last=20`) | `H=6 / A=5` | **`H=20 / A=19`** | 1 partido de Athletic se excluye as-of/cutoff; `P(1)` pasa de `0.3649 (L6)` -> `0.2994 (L10)` -> `0.3118 (L20)`. |
| **`7909836` (Inter Milano W vs Man City W)** (`outputs/matches/` vs caché `last=20`) | `H=6 / A=6` | **`H=20 / A=20`** | `P(1)` pasa de `0.3675 (L6)` -> `0.3891 (L10)` -> `0.3595 (L20)`. |
| **`7909841` (PSG W vs Bayern Munich W)** (`outputs/matches/` vs caché `last=20`) | `H=6 / A=6` | **`H=20 / A=20`** | `P(1)` pasa de `0.3961 (L6)` -> `0.3943 (L10)` -> `0.4192 (L20)`. |
| **`7905487` (Wales vs Portugal)** (`match_engine` congelado vs `preselection` 2026-11-17) | `H=6 / A=6` | **`H=16 / A=20`** (`sample_n` goles efectivo `15/19` tras rojas) | `P(Portugal)` pasa de `0.4237 (L6)` -> `0.4557 (L10)` -> `0.4703 (L20)`; sigue en `NO_BET` por gates intactos. |
| **`7909713` (Argentina vs Costa Rica) y `7909714` (Chile vs Colombia)** | `H=6 / A=6` | Permanecen **`6/6`** en su snapshot congelado | No entraron desde `preselection` ni tienen caché `last=20` local preexistente; en nuevas ejecuciones solicitarán `last=20` (aunque en selecciones la disponibilidad real puede seguir siendo `< 20` según calendario internacional). |
| **878 bundles únicos de `preselection`** (`COBERTURA_HISTORICA.csv`) | `790/878` ya traen `>=10` | `790/878 (90.0%)` con `>= 10` ambos bandos; `408/878` con `20/20` | **No se presupone** que el 90.0% de `preselection` se traslade automáticamente al flujo directo (copas/femenino/selecciones sin histórico API completo o filtros de tarjeta roja reducen la muestra útil). |

**Limitación operativa documentada:** los partidos `7..20` en modo `SCORE_ONLY_NO_STATS` (cuando `detail_limit = 6`) no disponen de línea temporal `events` ni estadística `Red Cards`, por lo que `red_card_distortion` solo se audita completamente en los `6` más recientes. Esto puede ajustarse por configuración (`PREMATCH_DEEP_HISTORY_DETAIL_LIMIT=20`) si en el futuro se requiere auditoría de tarjetas rojas sobre los `20` partidos.
