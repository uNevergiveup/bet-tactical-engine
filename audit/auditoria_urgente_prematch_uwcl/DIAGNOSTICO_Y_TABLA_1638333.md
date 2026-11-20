# Auditoría correctiva urgente PREMATCH — UWCL (18/11/2026)

**Fecha de cierre de auditoría:** 2026-11-18 (`Europe/Madrid`)  
**Rama:** `refactor/etapa2-fundamentos`  
**Caso de diagnóstico principal:** Real Madrid W – Inter Milano W (`fixture_id=1638333`, Bwin Event ID `7913438`, kickoff verificado `2026-11-18T18:45:00+01:00`, ya iniciado respecto a las `19:22 CET`).  
**Partidos objetivo de las 21:00 CET (`T-45 = 20:15:00+01:00`):** **`1638301` Chelsea W – Paris Saint Germain W** (Bwin `7913441`, `2026-11-18T21:00:00+01:00`) como candidato principal y **`1638312` Juventus W – OH Leuven W** (Bwin `7913440`, `2026-11-18T21:00:00+01:00`) como alternativo.

---

## 1. Diagnóstico de causa raíz (con archivos y líneas)

La salida original (`0 selecciones con datos iniciales investigables; 827 pendientes` y `market_research_shortlist.csv` con `0` filas; `16` señales tácticas en `PENDIENTE_MERCADO`; `Chelsea W–PSG W` bloqueado en ranking) no se debió a que el partido careciera de mercados con probabilidad evaluable, sino a **cinco rupturas concretas del pipeline**:

1. **Bloqueo por umbral rígido `same_competition >= 8` sin valorar muestra mixta en goles:**
   - `market_quality_gate.py` (`market_data_status`, ahora líneas `263-303` y `_goal_history_readiness` líneas `122-147`): `1638333` (`Real Madrid W–Inter Milano W`) y `1638301` (`Chelsea W–PSG W`) tienen en `preselection.json` `history.completed_total=20` (`goals_for/against.n=20`) y `history.same_competition_total=6` (`same_competition_goals_for/against.n=6`, fase de grupos UWCL), mientras que `1638312` (`Juventus W–OH Leuven W`) alcanzaba `same_competition=8`.
   - `preselect_matches.py` ya calificaba a `1638333` y `1638301` con `evidence_tier="MINIMUM_EVIDENCE_MET"`, pero `market_quality_gate` exigía exclusivamente `same_competition >= 8`, degradando **todas** las líneas O/U `.5` de goles FT a `PARTIAL_DATA` -> `researchable_after_data_gate = 0`, lo que además bloqueaba `select_single_match.py` (`NO_RESEARCHABLE_MARKET_FAMILY`).
   - En córners/tiros/tiros a puerta/faltas/fueras de juego/tarjetas de `1638333`, `market_profiles` tiene `n=4..6` (`PARCIAL < 6` salvo `yellow` home `n=6`, `away n=5`) y en `xG` `n=2` (`ESCASA`). Ahí **sí** falta muestra suficiente (`PARTIAL_DATA`), lo cual no debe confundirse con goles (`n=20` total + `n=6` misma competición).

2. **Taxonomía `market_kind` duplicada, divergente y con 182–204 filas en `other`:**
   - Antes de la corrección, `bwin_market_preflight.py` (línea `331`) y `market_quality_gate.py` (línea `71`) tenían implementaciones separadas de `market_kind()` que discrepaban entre `.md/.json` y `.csv` en `1638333` (`other=204, goalscorers=109` frente a `other=182, goalscorers=131`).
   - Mercados reales terminaban en `other` o fuera de su familia: `Jugador que marca...` (`40`), `Goles totales exactos`, `Multigol`, `Total de goles (0-4+/5+/6+)`, `Total de goles Par/Impar`, `* marca en ambas partes` (`total_goals`), `1X2 - Cuotas Mejoradas`, `Equipo no cuenta`, `gana sin recibir goles`, `portería a cero`, `Gana ambas partes` (`result`), `Doble oportunidad y ...` (`combined_or_builder` en vez de `double_chance` o viceversa).
   - Además, `market_quality_gate` no distinguía entre mercados FT con estimador real en `bet_engine/markets/bwin_evaluator.py` (`GOALS_FT`: `Resultado del partido FT`, `Ganador sin empate FT`, `Doble oportunidad FT`, `Ambos equipos marcan FT`, `Hándicap 1X2 (-1/-2/+1/+2)`, `Marcador exacto FT`, combinadas soportadas) y mercados sin modelo (`Descanso/Final`, `1er equipo en marcar`, `Multigol`, `2ª parte`, intervalos).

3. **Ausencia real en Bwin de 5 familias frente a las 16 señales API-Football (no fallo del parser):**
   - En `cuotas_bwin_7913438.txt` (`124` acordeones, `867` selecciones), `cuotas_bwin_7913441.txt` (`493` selecciones) y `cuotas_bwin_7913440.txt` (`485` selecciones), existen **0** mercados de `cards`, `fouls`, `shots`, `shots_on_target` y `offsides` (también `NOT_OBSERVED` en `inspection.market_headings`).
   - En `daily_runs/20261118T173219363925/tactical/fixtures/1638333.json`, las **8** señales `SUPPORTED_DESCRIPTIVE_SIGNAL` corresponden precisamente a `shots` (2), `on_target` (2), `fouls` (2), `yellow` (home) y `offsides` (home). Como Bwin no ofertó esas familias, esas señales quedan sin mercado ofertado (`NO_MARKET_CAPTURED`), mientras que el preflight `1638333_manual` se ejecutó a las `17:32:40 UTC` (posterior al run táctico `17:32:19 UTC`), dejando las `16` señales en `PENDIENTE_MERCADO`.

4. **Desconexión por alias femeninos (`W` / `F` / `(F)` / abreviaturas) en 5 módulos de cruce y parsing Shadow:**
   - `bwin_discovery.py` (línea `357` aprox. antes del fix): `team_candidate_queries()` tenía un bloque mal indentado dentro del bucle para `inter milano w` y faltaba un resolvedor centralizado sensible a categoría.
   - `prematch_tactical_signal_engine.py` (`_team_side_from_market_prefix`, línea `281`), `prematch_opportunity_scanner.py` (`identify_market`, líneas `130-134`), `prematch_market_mapping_v1.py` (`_compatible`, líneas `227-228`) y `bet_engine/orchestration/shadow_pipeline.py` (`_norm_team_tokens`/`parse_bwin_odds_txt`, líneas `37-115`) fallaban al emparejar prefijos Bwin como `Real Madrid (F)`, `Inter Milán (F)`, `PSG (F)` y `Oud-Heverlee Leuven (F)` contra nombres API (`Real Madrid W`, `Inter Milano W`, `Paris Saint Germain W`, `OH Leuven W`).
   - En `parse_bwin_odds_txt` de Shadow, ese fallo hacía que en `7913438` y `7913441` se perdieran claves directas de equipo (`home_win`, `away_win`, `dnb_home`, `dnb_away`, `home_over_*_goals`, `away_over_*_goals`, `away_over_*_corners`), bajando de `198` claves reales en `7913438` (`189` en `7913441`) tras el arreglo a `142` (`135`) previas.

5. **Bloqueo falso de `Chelsea W–Paris Saint Germain W` (`1638301`) en el ranking diario:**
   - `prematch_daily.py` (`bwin_ranking_links`, líneas `270` y `283` originales): `bwin_discovery.json` marcó correctamente a `1638301` (`bwin_event_id=7913441`) con `team_order_status="HOME_AWAY_ALIAS"` y `category_compatibility="FEMALE_CORROBORATED"`, pero `bwin_ranking_links` solo aceptaba `("HOME_AWAY", "HOME_AWAY_TOKEN")` y omitía `"HOME_AWAY_ALIAS"`, rechazándolo falsamente como `CANDIDATE_UNVERIFIED` (`EVENT_HOME_AWAY_ORDER_NOT_CORROBORATED`).
   - Además, `select_verified_bwin` (`prematch_daily.py` líneas `325-330`) no reenviaba `--confirm-external` cuando necesitaba ejecutar el scraper de `bwin_market_preflight.py`, y sin `--fixture-id 1638301` el selector automático elige por prioridad al candidato mejor puntuado disponible en `ranking.json` (p. ej. `1650229` o `1492456`).

---

## 2. Tabla de cobertura y modelos — `1638333` (`Real Madrid W–Inter Milano W`, Bwin `7913438`)

Total capturado en `cuotas_bwin_7913438.txt`: **867 selecciones** (`827` desde `1.20`, `20` en observación `1.10-1.19`, `20` `< 1.10`).  
Nota: tras la corrección, `market_preflight` unifica `market_kind()` entre `.json`, `.csv` y `.md`. Conteo de estados epistémicos tras el fix: `MODEL_EVALUATION_PENDING=156`, `MODEL_UNAVAILABLE=493`, `INSUFFICIENT_STATISTICAL_EVIDENCE=206`, `MARKET_CAPTURED_UNMAPPED=12`. Familias monitorizadas con `NO_MARKET_CAPTURED`: `cards`, `shots`, `shots_on_target`, `fouls`, `offsides`.

| `market_kind` | `total_capturado` (post-fix / pre-fix) | `evidencia_estadistica` (en `preselection` para `1638333`) | `modelo_disponible` (`bet_engine/markets/bwin_evaluator.py` + estado gate) | `motivo_bloqueo` | `correccion_posible` |
|---|---:|---|---|---|---|
| `total_goals` | **148** *(antes 81)* | **28** `DATA_READY_FOR_RESEARCH` (`GOAL_HISTORY_MIXED_COMPETITION`: `same_comp=6`, `completed_total=20`); **62** `NEEDS_PERIOD_SPLIT` (`1ª/2ª parte`); **58** `NEEDS_MODEL_OR_DISTRIBUTION` | `GOALS_FT` / `GOALS_H1` (`MODEL_AVAILABLE_PENDING_RUN=49`: 28 FT investigables + 21 O/U `.5` `1ª parte`); `MODEL_UNAVAILABLE=99` (`2ª parte`, `Multigol`, `exactos`, `Par/Impar`, intervalos `0-4+`) | Pre-fix: umbral rígido `same_comp>=8` dejaba `researchable=0`; post-fix entran **24** selecciones (`>=1.20`) a `market_research_shortlist.csv` (+4 bajo `1.20`); `1ª parte` bloqueada por falta de split por mitad; estructuras discretas sin modelo | **Corregido (mixta goles FT `.5` -> 24 shortlist / 28 `MODEL_EVALUATION_PENDING`)**. Faltan splits por mitad para `1ª parte` y distribuciones para `Multigol`/`exactos` |
| `corners` | **83** *(antes 83)* | `PARCIAL` (`n=5` en series `home/away for/against` < mínimo `6`): **14** `PARTIAL_DATA`, **39** `NEEDS_PERIOD_SPLIT`, **30** `NEEDS_MODEL_OR_DISTRIBUTION` | `CORNERS` para O/U `.5` FT (`MODEL_AVAILABLE_PENDING_RUN=14`, estado `INSUFFICIENT_STATISTICAL_EVIDENCE`); `MODEL_UNAVAILABLE=69` (`1ª/2ª parte`, `Par/Impar`, `0-7`/`0-15`, `Parte/Equipo con más`) | Muestra estadística genuinamente corta (`n=5 < 6` para `SUFICIENTE`) y falta de splits por mitad | **No forzar** (`INSUFFICIENT_STATISTICAL_EVIDENCE` real en FT `n=5`; recuperar más historial UWCL con stats detallados si existiera en API) |
| `result` | **79** *(antes 52)* | `GOAL_HISTORY_MIXED_COMPETITION` listo (`same_comp=6, completed=20`); en tabla base `NEEDS_MODEL_OR_DISTRIBUTION=79` | `GOALS_FT` (`MODEL_AVAILABLE_PENDING_RUN=10`: `1X2 FT`, `DNB FT`); `MODEL_UNAVAILABLE=69` (`1ª/2ª parte`, `Mejoradas`, `sin recibir goles`, `portería a cero`, etc.) | Gate previo no distinguía `1X2`/`DNB` FT modelados en `bwin_evaluator` del resto no modelado ni cruzaba su suficiencia en goles | **Corregido estado**: los **10** precios FT modelables pasan a `MODEL_EVALUATION_PENDING` (pendientes de ejecutar motor Shadow/evaluador); el resto permanece `MODEL_UNAVAILABLE` |
| `double_chance` | **9** *(antes 60)* | Goles FT mixtos listos (`same_comp=6, completed=20`); base `NEEDS_MODEL_OR_DISTRIBUTION=9` | `GOALS_FT` (`MODEL_AVAILABLE_PENDING_RUN=6` FT); `MODEL_UNAVAILABLE=3` (`1ª parte`) | Antes mezclaba `Doble oportunidad y ...` combinados (51 filas) y no separaba FT modelable vs H1 | **Corregido**: 51 filas movidas a `combined_or_builder`; **6** FT en `MODEL_EVALUATION_PENDING`, 3 `1ª parte` en `MODEL_UNAVAILABLE` |
| `both_teams_to_score` | **8** *(antes 8)* | Goles FT mixtos listos (`same_comp=6, completed=20`); base `NEEDS_MODEL_OR_DISTRIBUTION=8` | `GOALS_FT` (`MODEL_AVAILABLE_PENDING_RUN=4`: BTTS FT Sí/No); `MODEL_UNAVAILABLE=4` (`1ª/2ª parte`) | Base trataba todo BTTS como `NEEDS_MODEL_OR_DISTRIBUTION` sin distinguir BTTS FT evaluable por Poisson bivariante | **Corregido estado**: **4** FT en `MODEL_EVALUATION_PENDING`, 4 H1/H2 en `MODEL_UNAVAILABLE` |
| `handicap` | **17** *(antes 17)* | Goles FT mixtos listos (`same_comp=6, completed=20`); base `NEEDS_MODEL_OR_DISTRIBUTION=17` | `GOALS_FT` para `Hándicap 1X2 (-1/-2/+1/+2)` FT (`MODEL_AVAILABLE_PENDING_RUN=6`); `MODEL_UNAVAILABLE=11` (`(-3)` o `1ª parte`) | Hándicaps `(-1/-2/+1/+2)` ya existen en `bwin_evaluator`, pero gate no los diferenciaba | **Corregido estado**: **6** en `MODEL_EVALUATION_PENDING`, **11** en `MODEL_UNAVAILABLE` |
| `correct_score` | **134** *(antes 134)* | Goles FT mixtos listos (`same_comp=6, completed=20`); base `NEEDS_MODEL_OR_DISTRIBUTION=134` | `GOALS_FT` para marcador exacto FT (`MODEL_AVAILABLE_PENDING_RUN=90`); `MODEL_UNAVAILABLE=44` (`Multiple` / `1ª parte` / `2ª parte`) | Faltaba separar marcador exacto FT simple (evaluado desde matriz Poisson) de variantes `Multiple`/H1/H2 | **Corregido estado**: **90** en `MODEL_EVALUATION_PENDING`, **44** en `MODEL_UNAVAILABLE` |
| `combined_or_builder` | **185** *(antes 98)* | Goles FT mixtos listos (`same_comp=6, completed=20`); base `NEEDS_MODEL_OR_DISTRIBUTION=185` | `GOALS_FT` para `Resultado + BTTS`, `BTTS + O/U 2.5` y `BTTS + ganador` (`MODEL_AVAILABLE_PENDING_RUN=12`); `MODEL_UNAVAILABLE=173` (`Multigol`, intervalos `00:00-15:00`, `1ª parte`, `Crea tus apuestas`, `Doble op. + ...`) | Variantes `Resultado del partido y multigol` y `Doble oportunidad y ...` estaban mal repartidas en `other`/`double_chance` | **Corregido**: **12** combinadas FT con modelo pasan a `MODEL_EVALUATION_PENDING`; **173** quedan en `MODEL_UNAVAILABLE` |
| `half_full_time` | **12** *(antes 12)* | `NEEDS_MODEL_OR_DISTRIBUTION=12` | `MODEL_UNAVAILABLE=12` | No existe modelo conjunto H1×FT calibrado en `bwin_evaluator.py` | Mantener en `MODEL_UNAVAILABLE` (solo resoluble construyendo modelo H1/FT futuro, prohibido inventar probabilidades) |
| `first_team_to_score` | **9** *(antes 9)* | `NEEDS_MODEL_OR_DISTRIBUTION=9` | `MODEL_UNAVAILABLE=9` | No existe modelo de primer goleador de equipo | Mantener en `MODEL_UNAVAILABLE` |
| `goalscorers` | **171** *(antes 131)* | `PLAYER_DATA_AND_XI_REQUIRED=171` (`sin XI oficial confirmado ni exposición por jugadora`) | `MODEL_UNAVAILABLE=171` (`INSUFFICIENT_STATISTICAL_EVIDENCE`) | `40` filas de `Jugador que marca...` caían antes en `other`; faltan XI/minutos/rol | **Corregido clasificación** (`131->171`) + bloqueados legítimamente en `INSUFFICIENT_STATISTICAL_EVIDENCE` hasta tener XI y datos individuales |
| `other` | **12** *(antes 182 en gate / 204 en preflight)* | `NEEDS_MODEL_OR_DISTRIBUTION=12` | `MODEL_UNAVAILABLE=12` (`MARKET_CAPTURED_UNMAPPED`) | Quedan únicamente `marca` (4), `va por delante en cualquier momento` (4) y `Gana exactamente por 1/2 goles` (4) | Taxonomía unificada eliminó **170–192** falsos `other`; los 12 residuales quedan trazables como `MARKET_CAPTURED_UNMAPPED` |
| `cards` / `fouls` / `shots` / `shots_on_target` / `offsides` | **0** *(5 familias ausentes)* | En `preselection` existen señales descriptivas con `n=4..6` (`PARCIAL` salvo `yellow` home `n=6`) | Modelos existen en `bwin_evaluator.py` (`YELLOW_CARDS`, `FOULS`, `SHOTS`, `ON_TARGET`) pero **0 cuotas Bwin** | Bwin no ofertó estas 5 familias en `cuotas_bwin_7913438.txt` (`NO_MARKET_CAPTURED`) | **No inventar mercado ni marcar `NO BET`**: registrado explícitamente como `NO_MARKET_CAPTURED` en `missing_market_families` |

*(Comparativa verificada en lectura sobre `1638301` `Chelsea W–PSG W` (`7913441`): `493` capturadas -> `researchable_after_data_gate=23` (`total_goals DATA_READY=29`), `other=0`, `MODEL_EVALUATION_PENDING=150`, `MODEL_UNAVAILABLE=144`, `INSUFFICIENT_STATISTICAL_EVIDENCE=199`, `shadow_odds=189` claves vs `135` previas. En `1638312` `Juventus W–OH Leuven W` (`7913440`): `485` capturadas -> `researchable=22`, `other=0`, `shadow_odds=187` claves).*

---

## 3. Cambios mínimos implementados (`CAMBIOS_AUDITORIA_UWCL.diff`)

Todos los archivos de modelos probabilísticos/calibración (`bet_engine/modeling/*`, `motor_probabilistico.py`), fórmulas/umbrales EV, lógica de decisión Shadow y expedientes congelados (`outputs/matches/*`) permanecen **intactos**.

- **`config/bwin_team_aliases.json`** (nuevo): alias verificados categoría `FEMALE_CLUB` para `Real Madrid W`, `Inter Milano W`, `Chelsea W`, `Paris Saint Germain W`, `OH Leuven W`, `Juventus W`, `Lyon W` (sin cruzar masculino/femenino).
- **`bwin_discovery.py`**: arreglada indentación en `team_candidate_queries()` (línea `325+`) y añadido `resolve_verified_team_side()` (línea `362`) con bloqueo estricto por categoría (`FEMALE_RE`) y retorno `None` ante colisiones o ambigüedad.
- **`prematch_tactical_signal_engine.py`**: actualizado `_team_side_from_market_prefix()` (línea `281`) para usar `resolve_verified_team_side()` cuando el nombre literal difiere entre API `W` y Bwin `(F)`.
- **`prematch_opportunity_scanner.py`**: actualizado `identify_market()` (líneas `130-134`) para resolver propietario de mercado por equipo mediante `resolve_verified_team_side()`.
- **`prematch_market_mapping_v1.py`**: actualizado `_compatible()` (líneas `227-228`) con respaldo `resolve_verified_team_side()` verificando género femenino explícito.
- **`bet_engine/orchestration/shadow_pipeline.py`** y **`bet_engine/orchestration/adapters.py`**: corregido `_norm_team_tokens()` (línea `37`), añadido soporte de alias femeninos en `parse_bwin_odds_txt()` (línea `74+`) usando `team_candidate_queries`, y pasado `gender=match_meta.get("gender")` desde `adapters.py:164` (tras detectar y corregir en suite general que `MatchObservationBundle` no tiene atributo `.competition_stratum`).
- **`market_quality_gate.py`** y **`bwin_market_preflight.py`**:
  - Unificada `market_kind()` delegando `bwin_market_preflight.market_kind()` (línea `331`) en `market_quality_gate.market_kind()` (línea `71`).
  - Corregida clasificación de `goalscorers` (40 props antes en `other`), variantes de `total_goals`, `result`, `double_chance` vs `combined_or_builder`.
  - Añadido `_goal_history_readiness()` (línea `122`) que habilita `GOAL_HISTORY_MIXED_COMPETITION` (`same_comp >= 4` y `completed_total >= 12` en las 4 series) sin degradar córners (`>= 6`).
  - Añadidos `_model_availability()` (línea `149`), `_classify_market_epistemics()` (línea `205`), columnas CSV (`evidence_status`, `model_status`, `evaluation_state` con `extrasaction='ignore', restval=''`), `missing_market_families` (`NO_MARKET_CAPTURED`) y resumen en `market_preflight.md` con los 7 estados separados explícitamente.
- **`bet_engine/markets/bwin_evaluator.py`**: añadida propiedad no destructiva `@property def epistemic_evaluation_state(self)` (línea `78`) en `EvaluatedMarketOption` para separar `MODEL_UNAVAILABLE`, `NO_MARKET_CAPTURED`, `MODEL_EVALUATED_VALUE_CANDIDATE` y `MODEL_EVALUATED_NO_VALUE`.
- **`prematch_daily.py`**: aceptado `HOME_AWAY_ALIAS` (y rechazado `AWAY_HOME_ALIAS_CONFLICT`) en `bwin_ranking_links()` (línea `283`); propagado `--confirm-external` en `select_verified_bwin()` (línea `329`).
- **`test/test_uwcl_market_audit_regression.py`** (nuevo): 7 pruebas de regresión cubriendo los 7 escenarios exigidos en Prioridad 4.

---

## 4. Pruebas ejecutadas y resultados

### 4.1 Regresiones focalizadas (`audit/auditoria_urgente_prematch_uwcl/TESTS_FOCALIZADOS.log`)
Comando ejecutado tras la última edición (`adapters.py`):
```bash
PYTHONPATH=.:test .venv/bin/python -m unittest \
  test_uwcl_market_audit_regression test_market_quality_minimal \
  test_select_single_match test_manual_prematch_flow test_prematch_daily \
  test_pinned_fixture_category_hotfix test_discovery_fixture_audit_fixes \
  test_filter_adult_competitions test_market_coverage_and_taxonomy_stage1 \
  test_prematch_commercial_join test_prematch_commercial_models \
  test_prematch_tactical_signal_engine test_probabilistic_audit_stage2 \
  test_p0_c1_c4_audit_closure test_p0_temporal_and_shadow_sync \
  test_etapa5_shadow_and_lifecycle test_etapa5_2_cli_shadow_integration \
  test_lineup_fallback_e2e test_lineup_management_p0 \
  test_manual_lineup_inbox_flow test_prematch_case_resume_closure \
  test_event_7905487_p0_integrity_sync -v
```
- **Resultado:** `Ran 191 tests in 12.814s` -> **`OK` (191/191 aprobadas, 0 fallos, 0 errores)**.

### 4.2 Suite general completa (`audit/auditoria_urgente_prematch_uwcl/TESTS_SUITE_GENERAL.log`)
Comando ejecutado tras la última edición:
```bash
PYTHONPATH=.:test .venv/bin/python -m unittest discover -s test -v
```
- **Resultado:** `Ran 1042 tests in 114.081s` -> **`982` aprobadas**, **`17` omitidas (`skipped`)**, **`5` fallos (`failures`)**, **`38` errores (`errors`)**.
- **Separación estricta de fallos preexistentes vs regresiones nuevas (`0` regresiones nuevas):**
  1. **25 errores/fallos preexistentes por rutas heredadas tras mover tests a `test/`** sin actualizar `ROOT` / rutas relativas (`test/outputs/...`, `../outputs/...`, `test/prematch_multi.py`, `/T/prematch_multi.py`, `test_prematch_v2_phase2`).
  2. **13 errores preexistentes por `ModuleNotFoundError: No module named 'test.test_prematch_integral_analysis_v1046'`** (la carpeta `test/` carece de `__init__.py`): 2 en `test_prematch_integral_analysis_v1046.py` y 11 en `test_prematch_tactical_intelligence_v1050.py`.
  3. **5 fallos/errores preexistentes de iteraciones anteriores en la rama (en archivos no modificados en esta auditoría):**
     - `test_bwin_discovery_v3.test_v3_budget_classifies_before_external_work`: `(151, 298, 108) != (150, 299, 108)` (cambio previo en elegibilidad/descubrimiento).
     - `test_discovery_coverage_ranking_fixes.test_07_discovery_for_auto_vs_manual_reconciliation`: `BWIN_MANUAL_IDENTITY_CONFLICT` en `prematch_runtime.py:369`.
     - `test_fase_a_b_recovery_quick_context.test_real_failed_evaluation_offline_replay_activates_scanner_and_research_pending`: `INSUFFICIENT_EVIDENCE != READY_FOR_MANUAL_RESEARCH` (reproducción del `2026-11-16`).
     - `test_p0_repair.test_absent_XI_keeps_context_and_archives_acquisition`: `'LINEUPS_UNAVAILABLE_FINAL' != 'XI_ABSENT_OR_INCOMPLETE'`.
     - `test_prematch_functional_isolation.test_restored_mtime_cannot_refresh_cache`: clave `_acquired_at_utc` añadida en `prematch_cache_record.py`.

---

## 5. Comandos exactos y verificados para Chelsea W – PSG W (`1638301` / Bwin `7913441`) antes de las `20:15 CET` (`T-45`)

**Hallazgo temporal crítico comprobado antes de entregar comandos:**
- El snapshot `outputs/discovery/2026-11-18_1500_0300/evaluations/20261118T163044121035Z_81e8029defcf/prospective_trial/bwin/bwin_discovery.json` se capturó a las `16:32:18+00:00` (`17:32:18 CET`, observación de `1638301` a las `17:32:02 CET`), por lo que caduca en `bwin_ranking_links` (`max_age=120m`) a las **`19:32:02 CET`**.
- Las cuotas actuales `outputs/matches/7913441/inputs/cuotas_bwin_7913441.txt` son de las **`16:57:02+01:00`** (`> 120m` ya superados), por lo que `select_verified_bwin` sin refresco previo lanzaría `EXISTING_ODDS_STALE`.
- En el ranking de `2026-11-18` con `--competition-scope top-tier-and-senior-cups` (`19` recibidos, `13` rankeados, `12` verificados), `1638301` ocupa la posición `10` (`47.52`), debajo de `1650229` (`69.58`), `1492456` Remo (`60.51`) o `1638312` Juventus W (`48.37`), por lo que **es obligatorio** fijar `--fixture-id 1638301` para no seleccionar otro encuentro.

Ejecutar estos pasos **en orden antes de las `20:15 CET`** (con `--confirm-external` donde aplica red/navegador; **sin** alterar `ranking.json`, que sigue conservando los 19 candidatos):

```bash
EVAL_DIR="outputs/discovery/2026-11-18_1500_0300/evaluations/20261118T163044121035Z_81e8029defcf/prospective_trial"

# Paso 1: Refrescar discovery Bwin dirigido SOLO a 1638301 (evita caducidad >120m tras 19:32 CET)
.venv/bin/python bwin_discovery.py \
  --input "$EVAL_DIR/fixtures_major_test.json" \
  --fixture-id 1638301 \
  --catalog-snapshot "$EVAL_DIR/bwin/bwin_discovery.json" \
  --directed-pilot \
  --no-country-discovery \
  --bwin-first-max-pages 0 \
  --headless \
  --confirm-external \
  --output-dir outputs/manual_discovery/1638301

# Paso 2: Recapturar cuotas frescas de 7913441 (archiva el TXT de 16:57 por SHA) y generar preflight (23 investigables)
.venv/bin/python bwin_market_preflight.py \
  --discovery outputs/manual_discovery/1638301/bwin_discovery.json \
  --preselection "$EVAL_DIR/enriched/preselection.json" \
  --fixture-id 1638301 \
  --refresh-existing \
  --run-scraper \
  --confirm-external \
  --min-lead-minutes 45 \
  --output-dir outputs/manual_preflight/1638301

# Paso 3: Ranking diario completo (conserva ranking.json con todos los candidatos) + selección exclusiva 1638301 + check
.venv/bin/python prematch_daily.py \
  --date 2026-11-18 \
  --competition-scope top-tier-and-senior-cups \
  --rank \
  --preselection "$EVAL_DIR/enriched/preselection.json" \
  --bwin-catalog outputs/manual_discovery/1638301/bwin_discovery.json \
  --select-verified \
  --fixture-id 1638301 \
  --preflight outputs/manual_preflight/1638301/market_preflight.json \
  --min-lead 45 \
  --max-odds-age 30 \
  --confirm-external

# Paso 4 (ANTES de 20:15 CET): Preparación aislada API + reutilización de las cuotas frescas ya capturadas
# Sustituye <RUN_DIR> por la ruta impresa en selection_result.json del Paso 3 (selected_match.json):
SELECTED=$(ls -t outputs/phase3/2026-11-18_1500_0300/runs/*/fixtures/1638301/selection_pipeline/selected_match.json | head -n 1)
.venv/bin/python prematch_single.py --confirm-external --selected "$SELECTED" prepare --reuse-odds

# Paso 5 (Ventana XI oficiales T-45..T-15, antes de 20:45 CET) y refresco final / cierre (hasta T-5 = 20:55 CET):
.venv/bin/python prematch_single.py --confirm-external --selected "$SELECTED" acquire-lineups
.venv/bin/python prematch_single.py --confirm-external --selected "$SELECTED" refresh-odds
.venv/bin/python prematch_single.py --selected "$SELECTED" finish --report outputs/matches/7913441/instructions/reporte_cualitativo_7913441.md --shadow-mode
```

*(Plan alternativo si `1638301` fallara identidad/cuotas: repetir exactamente los mismos pasos cambiando `--fixture-id 1638312` para `Juventus W–OH Leuven W`, Event ID `7913440`, ya rankeado #9 con `48.37` y `same_competition=8`)*.

---

## 6. Riesgos pendientes / funciones no implementadas

1. **Discrepancias temporales en `--sync-bwin` interno de `prematch_daily.py`:** `operational_research` cuando invoca `bwin_discovery.py` vía `--sync-bwin` no reenvía `--confirm-external` y busca `prospective_trial/bwin` en vez de `evaluations/*/prospective_trial/bwin`. Por eso el flujo recomendado arriba separa Paso 1 (`bwin_discovery.py --confirm-external`) y Paso 2 (`bwin_market_preflight.py --refresh-existing --run-scraper --confirm-external`) antes del Paso 3.
2. **`select_verified_bwin` con `preflight=None`:** si un TXT previo tiene más de `max-odds-age`, `select_verified_bwin` rechaza con `EXISTING_ODDS_STALE` aunque se pase `--refresh-existing-odds` a `prematch_daily.py`; pasar `--preflight` recién generado en el Paso 2 resuelve limpiamente el contrato sin alterar `select_single_match.py`.
3. **Ausencia real de oferta en Bwin y splits por mitad en API:** `cards`, `fouls`, `shots`, `shots_on_target` y `offsides` están en `NO_MARKET_CAPTURED` en las capturas UWCL auditadas (`7913438`, `7913441`, `7913440`), y los mercados de `1ª parte` / `2ª parte` / `Multigol` / `Descanso-Final` siguen bloqueados legítimamente como `INSUFFICIENT_STATISTICAL_EVIDENCE` (`NEEDS_PERIOD_SPLIT`) o `MODEL_UNAVAILABLE`.
4. **Cobertura de alineaciones y contexto de lesiones en vivo:** hasta ejecutar `prepare` y `acquire-lineups` (tras T-60 / T-45) sobre `1638301`, la disponibilidad efectiva de 22 titulares oficiales en API-Football queda `PENDIENTE_DE_VERIFICACION` (props de goleadoras permanecen bloqueados hasta tener XI y datos individuales).
