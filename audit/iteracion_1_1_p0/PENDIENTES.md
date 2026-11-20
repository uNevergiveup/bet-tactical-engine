# PENDIENTES — Deuda técnica abierta tras Iteración 1.1

## 1. Deuda funcional y probabilística reservada para Iteración 2 (NO implementada aquí)

Estado verificado directamente en el código fuente en Iteración 1 / 1.1:

| Ítem | Estado de verificación | Ubicación observada | Severidad | Impacto |
|---|---|---|---|---|
| `calibration_accredited_families` vacío por defecto (`frozenset()`) | **OBSERVADO EN CÓDIGO** | `bet_engine/markets/bwin_evaluator.py:124-146, 346, 356` | Crítica | `calibration_accredited` es siempre `False` en producción; impide alcanzar `PICK_ACCIONABLE` aunque haya `ev_lo > 0` y XI confirmados. |
| Requisito `sample_n >= 10` frente a históricos de 6 partidos | **OBSERVADO EN CÓDIGO Y DATOS** | `bet_engine/markets/bwin_evaluator.py:237`; `outputs/matches/7909713/shadow/shadow_summary_7909713.json` (`raw_sample_n=6`) | Alta | Bloquea `PICK_ACCIONABLE` (`BLOQUEO_ACCIONABLE_MUESTRA_CORTA(n<10)`) en expedientes con `deep_history_limit=6`. |
| Históricos limitados a 6 encuentros detallados en `match_engine_<eid>.json` | **OBSERVADO EN DATOS Y CÓDIGO** | `outputs/matches/7909713/inputs/match_engine_7909713.txt:24` (`Deep history limit: 6`); `ObservationStore` | Alta | Reduce `n_eff` (~4.45–4.55 en `7909713`) y ensancha los intervalos Gamma-Poisson. |
| Prior plano `STRATUM_PRIORS["NATIONAL_TEAM"]["goals"] = (1.50, 1.40)` con `shrink_k=8.0` | **OBSERVADO Y REPRODUCIDO** | `bet_engine/modeling/distributions.py:344-345, 404, 490` | Alta | Con `w_sum` de `4.0–5.0`, el prior recibe >61 % de peso y comprime la asimetría real (`7909713`: `lambda_home=2.1916`, `lambda_away=1.0162` -> `63.72 %` vs `@1.055` Bwin). |
| Ausencia de ponderación directa por calidad del rival | **OBSERVADO EN CÓDIGO** | `bet_engine/modeling/distributions.py:393-525` | Alta | `_estimate_team_rate_detailed` solo pondera competición, tarjeta roja y deduplicación H2H; no ajusta por Elo/ranking/fuerza del oponente. |
| Calibración out-of-sample por familia de mercado | **OBSERVADO COMO PENDIENTE** | `bet_engine/modeling/calibration.py` existe como evaluador walk-forward, sin tablas calibradas conectadas a `bwin_evaluator.py` | Alta | Sin calibración validada fuera de muestra, no es defendible acreditar familias en producción. |
| Integración de LLM táctico y evaluación independiente de Codex | **OBSERVADO EN CÓDIGO** | `generar_prompt_gemini.py`, `generar_prompt_grok.py`, `prematch_llm_research.py` | Media | Actualmente solo generan prompts/paquetes offline o validan hipótesis estructuradas contra holdout, sin integración directa en el flujo. |
| Histórico NO BET en `outputs/matches/` (100 % NO BET en 6 Shadow / 8 Codex) | **OBSERVADO EN `outputs/matches/`** | `outputs/matches/*/shadow/` (6/6 `NO_BET`), `analysis/` (8/8 `NO BET`), `validation/` (1 `BLOCK` en `7909713`) | Informativa | Coexistencia de causas estadísticas (`ev_lo < 0`), estructurales (`calibration_accredited=False`, `sample_n < 10`) y bloqueo técnico (`7909713`). |

---

## 2. Fallos y errores preexistentes de la suite heredada `test/` (54 total)

Ver detalle completo en [`REGRESIONES.md`](./REGRESIONES.md). No fueron introducidos por P0 (`0` regresiones respecto al baseline `task-12`):

- **17 `FAIL` preexistentes** (ej. `test_filter_adult_competitions`, `test_p0_repair`, `test_prematch_functional_isolation`, `test_discovery_fixture_audit_fixes`, `test_prematch_reuse_odds_contract`, etc.).
- **37 `ERROR` preexistentes** (en su mayoría `ValueError` / `FileNotFoundError` por rutas relativas a `test/` heredadas al mover los ficheros de test desde la raíz del repositorio a `test/`).

---

## 3. Nota sobre `prematch_multi.py`

- `prematch_multi.py` gestiona `prepare`, `finish`, `retry-context` y `retry-scraper` (solo cuando aún no existen cuotas válidas en `manifest`), y ahora escribe los timestamps de precios y probabilidades Shadow en `CODEX_<EVENT_ID>.md` tras `finish`. No posee un subcomando `refresh-odds` propio (dicho flujo pasa por `prematch_case.py` -> `prematch_single.py:cmd_refresh` o `refresh_odds_from_snapshot`).
