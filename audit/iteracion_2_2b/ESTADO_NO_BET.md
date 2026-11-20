# Seguimiento de Decisiones `NO BET` en Expedientes Reales — Iteración 2.2B

**Fuente auditada:** `159` directorios reales de eventos bajo `outputs/matches/` (excluyendo ejecuciones sintéticas de `test/`).

---

## 1. Contadores independientes y denominadores explícitos

| Categoría | Conteo | % sobre total (`159`) | % sobre completos (`10`) | % sobre evaluados Shadow (`6`) | Ejemplos reales |
|---|---|---|---|---|---|
| **1. Expedientes incompletos** (sin `match_engine_*.json` + `cuotas_bwin_*.txt` simultáneos) | **`149`** | `93.71%` | `—` | `—` | `7900504`, `7904043`, `7904520` |
| **Expedientes completos (`match_engine` + `cuotas_bwin`)** | **`10`** | `6.29%` | `100.0%` | `—` | — |
| ↳ Completos sin `shadow_comparison_*.json` operativo | `4` | `2.52%` | `40.00%` | `—` | `7903872`, `7909836`, `7909841`, `7911900` |
| **2. Fallos operativos** (JSON ilegible / fallo técnico de Shadow) | **`0`** | `0.00%` | `0.00%` | `0.00%` | `—` |
| **3. Bloqueos por controles PREMATCH** (validación/temporalidad/identidad) | **`1`** | `0.63%` | `10.00%` | **`16.67%`** | `7909713` |
| **4. `NO BET` estadísticos de expedientes completos** | **`5`** | `3.14%` | `50.00%` | **`83.33%`** | `7905477`, `7905487`, `7909704`, `7909714`, `7912816` |
| **5a. Candidatos exploratorios/condicionales** (subconjunto de los `5` anteriores) | **`3`** | `1.89%` | `30.00%` | `50.00%` (`60.00%` de los 5 `NO BET` estadísticos) | `7905487`, `7909714`, `7912816` |
| **5b. Picks accionables** | **`0`** | `0.00%` | `0.00%` | `0.00%` | `—` |

**Lectura operativa:** en los expedientes reales completamente evaluados por Shadow (`6`), no hay fallos técnicos (`0`), `16.67%` (`1/6`) quedaron bloqueados por controles PREMATCH (`7909713`) y `83.33%` (`5/6`) terminaron en `NO BET` estadístico (con `3/5` aportando candidatos condicionales bloqueados por `sample_n < 10`, falta de acreditación o ausencia de XI confirmado).
