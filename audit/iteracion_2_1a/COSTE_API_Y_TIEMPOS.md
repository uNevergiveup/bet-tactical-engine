# Coste en API-Football, Tamaño de Payloads y Tiempos de Ejecución — Iteración 2.1A (Fase B)

**Proyecto:** `bet-tactical-engine`  
**Configuración medida:** `DEEP_HISTORY_LIMIT = 20`, `DEEP_HISTORY_DETAIL_LIMIT = 6`, `FIXTURE_IDS_BATCH_SIZE = 20`.

---

## 1. Incremento de peticiones a API-Football (`B3`)

| Paso de captura profunda (`collect_team_history`) | Antes (`limit=6, detail=6`) | Después (`limit=20, detail=6`) | Alternativa descartada (`limit=20, detail=20`) |
|---|---|---|---|
| Listado reciente por equipo (`/fixtures?team={id}&last=...`) | `1` petición × equipo (`2` total) | `1` petición × equipo (`2` total) | `1` petición × equipo (`2` total) |
| Detalle por lote (`/fixtures?ids=...`, hasta 20 IDs/lote) | `1` batch de `6` IDs × equipo (`2` total) | `1` batch de `6` IDs × equipo (`2` total) | `1` batch de `20` IDs × equipo (`2` total) |
| **Total peticiones API-Football de historial** | **`4` peticiones** | **`4` peticiones (+0 extra)** | `4` peticiones (pero ×3.3x bytes de respuesta) |

Además, [`APIFootballClient.get`](file:///Users/antonio/Projects/bet-tactical-engine/generate_context.py#L1040-L1046) reutiliza automáticamente el caché local (`outputs/data/cache/`) cuando ya existe `/fixtures?team={id}&last=20` generado previamente (p. ej. `123` equipos ya cacheados en local), evitando incluso nuevas llamadas remotas para esos equipos dentro del TTL vigente.

---

## 2. Tamaño en bytes y tiempo de ejecución medidos localmente

1. **Pesos reales observados en `outputs/data/cache/`:**
   - Respuesta `/fixtures?team={id}&last=20` (`n = 123` archivos): promedio **`31,608 B` (`~30.9 KB`)** por equipo.
   - Respuesta batch `/fixtures?ids=...` (`n = 6` fixtures, `111` archivos): promedio **`585,707 B` (`~572.0 KB`)** por equipo (`~97.6 KB` por fixture detallado). Batch `10` (`1` archivo): `1,073,202 B` (`~107.3 KB` por fixture).
   - Evitar `detail_limit = 20` para los `14` partidos adicionales ahorra aproximadamente **`2 × 14 × ~98 KB ≈ 2.7 MB`** de descarga y serialización por evento cuando `statistics/events` no se requieren más allá de P-1..P-6.
2. **Delta exacto medido sobre `match_engine_*.json` reales y latencia Shadow end-to-end (`ObservationStore` + `Stage3` + `CoherentMatchModeler` L6/L10/L20 + `BwinMarketEvaluator`):**

| Evento | Tamaño JSON (`L6 -> L20+detail6`) | Partidos Shadow (`H/A`) | Latencia local L6 vs L20 (`ms`) | Decisión Shadow |
|---|---|---|---|---|
| `7903872` (Osasuna vs Athletic Club) | `710 KB -> 740 KB` (**`+29 KB`**, `+4.1%`) | `6/5 -> 20/19` | `27.2 ms -> 27.7 ms` (**`+0.5 ms`**) | `NO_BET` |
| `7909836` (Inter W vs Man City W) | `600 KB -> 631 KB` (**`+30 KB`**, `+5.0%`) | `6/6 -> 20/20` | `25.7 ms -> 27.8 ms` (**`+2.1 ms`**) | `NO_BET` |
| `7909841` (PSG W vs Bayern W) | `591 KB -> 621 KB` (**`+30 KB`**, `+5.0%`) | `6/6 -> 20/20` | `25.9 ms -> 27.0 ms` (**`+1.1 ms`**) | `NO_BET` |

**Conclusión operativa (B3):** pasar a `DEEP_HISTORY_LIMIT = 20` con `DEEP_HISTORY_DETAIL_LIMIT = 6` tiene **coste `0` en peticiones API-Football**, añade **`~30 KB`** al `match_engine_*.json` y **`~1–2 ms`** al cálculo completo de Shadow (incluyendo la comparativa L6/L10/L20), manteniéndose completamente viable dentro de la ventana PREMATCH.
