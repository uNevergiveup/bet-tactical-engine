# AUDITORÍA CORRECTIVA INTEGRAL V1.0 — BET ENGINE

Fecha de cierre de auditoría: **2026-11-17 (Europe/Madrid, CET UTC+1)**  
Rama de trabajo: `refactor/etapa2-fundamentos`  
Estado final de liberación operativa: **`READY_FOR_CONTROLLED_PREMATCH_TRIAL`**

---

## A. Resumen ejecutivo

Se ejecutó una intervención correctiva autónoma y verificable (Fases A–H) sobre **Bet Engine** en modo estrictamente offline (`0` llamadas a scraper Bwin, `0` peticiones a API-Football, `0` navegación web y `0` alteraciones sobre los expedientes congelados en `outputs/matches/7909704/` y `outputs/matches/7912816/`).

La auditoría resolvió seis defectos estructurales detectados tras las dos últimas pruebas reales completas (`Estonia vs Bulgaria · Bwin 7909704` y `Montevideo Wanderers FC vs Juventud de Las Piedras · Bwin 7912816`):

1. **Ingesta cualitativa y validación (`prematch_case.py`, `validate_prematch.py`, `bet_engine/context/research_parser.py`)**: el validador exigía literales rígidos (`Event ID:`, `Fixture ID:`) e ignoraba variantes documentadas (`EVENT_ID_BWIN`, `fixture_id API-Football`), descartaba horarios ISO con sufijo `, CET.` y omitía las tablas paralelas de **11+11 XI `PROBABLE`**. Ahora acepta las variantes válidas, rechaza IDs ausentes/contradictorios o de otro partido, preserva `REPORT_COMPLETION_UNKNOWN` cuando falta `report_generated_utc` y extrae XI `PROBABLE` sin promoverlos nunca a titulares oficiales.
2. **Entrenadores y alertas contextuales (`bet_engine/context/observation_store.py`, `bet_engine/analytics/plugins.py`)**: la selección de entrenador tomaba el primer registro histórico (`Apud / Tiscornia` en lugar de `Mathías Corujo / Sergio Blanco`) y un partido de Copa sin confirmar (`1649971`, `venue=null`) disparaba `FIXTURE_CONGESTION_ROTATION_RISK`. Se implementó resolución cronológica y canónica (`CURRENT_VERIFIED`, `CURRENT_PROBABLE`, `HISTORICAL`, `UNKNOWN`), separación explícita de evidencia verificada vs componente inferido y bloqueo de falsas congestiones coperas (`PROVISIONAL_CUP_LOOKAHEAD_UNCONFIRMED`).
3. **Audit probabilístico y dispersión (`bet_engine/modeling/distributions.py`, `bet_engine/modeling/calibration.py`)**: la validación fuera de muestra (`1.753` bundles, `dev=1.305 / strict_holdout=1.363` equipos no vistos) demostró que `shrink_k=8.0` supera de forma consistente a priors débiles (`k=3..5`), evitando el sobreajuste ad hoc a `7912816`. A la vez, se corrigieron tres fallos reales: activación de Binomial Negativa desde `n_obs >= 4` (antes `>= 5` bloqueaba ventanas detalladas de 4 partidos), cálculo de sobredispersión con `sample_mean` en lugar del `lam` encogido, alerta `PRIOR_WEIGHT_EXCEEDS_LOCAL_SAMPLE` cuando la regularización domina la muestra local, y coherencia exacta entre rejilla conjunta FT/H1 y `Under 0.5 == Exact 0-0`.
4. **Equivalencias contractuales y ampliación de mercados Bwin (`bet_engine/markets/market_equivalence.py`, `bwin_evaluator.py`, `shadow_pipeline.py`)**: se construyó el motor de contratos canónicos que detecta equivalencias exactas de liquidación (p. ej. `exact_score_0_0 @ 11.50` vs `under_0_5_goals @ 10.50`, `+9.52%`) y prohíbe falsos equivalentes (`DNB` vs `EH -1`, `FT` vs `1H`). Con la matriz Dixon-Coles FT/H1 coherente, la cobertura cotizada pasó en `7909704` de **15 a 151** selecciones modeladas y en `7912816` de **68 a 167**, protegiendo colas `<5%` para que nunca escalen a `PICK_ACCIONABLE`.
5. **Ranking y motor táctico descriptivo (`prematch_tactical_signal_engine.py`)**: se corrigió el umbral plano `CV > 0.8` en recuentos discretos de media baja mediante cota Poisson-aware, la resolución de prefijos Bwin por equipo (`Montevideo Wanderers FC - Total de goles`) y el cero espurio de `coverage_summary` (`0 -> 5` observaciones válidas por familia útil; score de `1649063`: `0 -> 74.69`).
6. **Pruebas y verificación**: las **19 suites activas objetivo** (`175` tests) pasan íntegramente (`OK`, `skipped=1` por artefacto congelado externo de Pasto–Junior). El descubrimiento completo heredado (`917` tests) mantiene fallos y errores preexistentes explicados en la Sección E.

---

## B. Registro de cambios por archivo, función y defecto

| Fase | Archivo y símbolo | Defecto y causa raíz | Solución implementada | Prueba verificada y resultado |
|---|---|---|---|---|
| **B** | [`research_parser.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/research_parser.py) (`parse_research_identity_and_metadata`, `validate_research_identity`, `_extract_parallel_xi_table`, `_extract_scalar_pairs`) | Parser rígido limitado a `Event ID:` / `Fixture ID:` y sin lector de fechas `, CET.` ni tablas paralelas de prensa. | Normalización de variantes clave (`EVENT_ID_BWIN`, `fixture_id API-Football`), rechazo de duplicados contradictorios (`CONFLICTING_EVENT_IDS`), parseo ISO con offset, conservación de `generated_utc=None`, extracción `11+11 PROBABLE` y atribución Grok/Perplexity/Codex. | [`test_corrective_audit_v10.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_corrective_audit_v10.py) (`test_fase_b_*`, `test_adverse_*`): **OK** |
| **B** | [`prematch_case.py`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_case.py#L436-L460) (`attach_research`) y [`validate_prematch.py`](file:///Users/antonio/Projects/bet-tactical-engine/validate_prematch.py#L766-L925) (`report_meta`, `audit_report`) | Bloqueaba informes válidos sin etiquetas manuales y omitía metadatos de provenance en `manifest.json`. | Integración de `validate_research_identity` y `report_meta`; falla cerrado ante identidad ausente o de otro evento (`QUALITATIVE_RESEARCH_IDENTITY_MISMATCH`), preserva referencias SHA-256 y `REPORT_COMPLETION_UNKNOWN`. | [`test_prematch_case.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_prematch_case.py) + `test_corrective_audit_v10.py`: **OK** (`1` skip en artefacto externo Pasto–Junior) |
| **C** | [`observation_store.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py#L864-L1055) (`_resolve_coach_regime`, `_canonical_coach_list`, `_same_coach_person`, `SCHEDULE_FATIGUE`) | Se tomaba el entrenador más antiguo (`first` del listado) y se marcaba congestión ante próximos cruces sin estadio ni confirmación (`venue=null`). | Selección cronológica descendente desde actas más recientes (`CURRENT_VERIFIED` / `CURRENT_PROBABLE` / `HISTORICAL` / `UNKNOWN`), deduplicación de variantes de ID/nombre y banderas `next_fixture_venue_present` / `next_fixture_confirmed`. | [`test_etapa2_contracts_and_store.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_etapa2_contracts_and_store.py) + `test_fase_c_*`: **OK** |
| **C** | [`plugins.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/analytics/plugins.py#L50-L795) (`RefereeAndScheduleContextPlugin`) | Alertas contextuales mezclaban hechos observados con inferencias y disparaban rotación falsa por Copa aplazada (`1649971`). | Campos explícitos `supporting_evidence`, `inferred_component` y `market_relevance`; emisión de `PROVISIONAL_CUP_LOOKAHEAD_UNCONFIRMED` cuando `venue=null` / sin confirmación. | [`test_etapa3_analytics_core.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_etapa3_analytics_core.py) + `test_fase_c_*`: **OK** |
| **D** | [`distributions.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py#L57-L320) (`build_count_pmf`, `prob_over`, `joint_grid_with_corners`) | NegBin exigía `n_obs >= 5` y usaba `raw_var / lam` (distorsión de sobredispersión con prior fuerte); faltaba aviso de dominio del prior; falta de coherencia `H1`/`Under 0.5`. | Activación NegBin desde `n_obs >= 4`, `raw_phi = raw_var / max(sample_mean, 0.1)`, advertencia `PRIOR_WEIGHT_EXCEEDS_LOCAL_SAMPLE`, rejilla coherente H1 y coincidencia `grid[0][0] == prob_over('total', 0.5)['p_under']`. | [`test_probabilistic_audit_stage2.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_probabilistic_audit_stage2.py) + `test_fase_d_*`: **OK** |
| **D** | [`calibration.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py#L98-L900) (`evaluate_interval_calibration`, `evaluate_stage1_vs_stage2`, `evaluate_joint_match_walk_forward`) | Métrica de cobertura podía confundir bandas de tasa esperada con cobertura de recuento bruto sin desagregar sesgo por decil. | Incorpora `count_interval_coverage_80`, `max_abs_bin_bias`, auditoría por 10 deciles y nota explícita `RATE_BAND_BIN_CONTAINMENT_NOT_EQUATED_TO_FULL_CALIBRATION`. | `test_probabilistic_audit_stage2.py` + corridas Walk-Forward corpus (`1.753` bundles): **OK** |
| **E** | [`market_equivalence.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/markets/market_equivalence.py#L27-L220) (`CanonicalContract`, `canonicalize_selection_key`, `are_contracts_equivalent`, `audit_snapshot_equivalences`) | No existía comparación entre mercados idénticos en liquidación (`Exact 0-0` vs `Under 0.5 FT`, `1-1` vs `BTTS Sí & Under 2.5`, `EH +1` vs `DC`). | Registro de predicados contractuales y periodo/regla; detecta arbitrajes de precio entre variantes equivalentes y bloquea falsos equivalentes (`FT` vs `H1`, `DNB` vs `EH -1`). | `test_fase_e_*` + inspección `7909704` y `7912816`: **OK** |
| **E** | `bwin_evaluator.py`, `shadow_pipeline.py` (parseo Bwin y evaluación estándar/derivados FT/H1) | Solo `68/256` cotizadas evaluadas en `7912816` (`15` en `7909704`); mercados H1, Marcador Exacto FT/H1, Hándicap Europeo y combinados BTTS excluidos. | Expansión matemática desde distribuciones coherentes FT/H1 (`151/397` en `7909704`, `167/256` en `7912816`), anotación de mejor precio equivalente y bloqueo `p_model < 0.05 -> CANDIDATO_CONDICIONAL` (nunca `PICK_ACCIONABLE`). | [`test_market_coverage_and_taxonomy_stage1.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_market_coverage_and_taxonomy_stage1.py) + [`test_etapa4_probabilistic_and_markets.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_etapa4_probabilistic_and_markets.py) + `test_fase_e_*`: **OK** |
| **F** | [`prematch_tactical_signal_engine.py`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_tactical_signal_engine.py#L251-L450) (`_team_side_from_market_prefix`, `classify_market`, `coverage_summary`, `analyze_fixture`) | Umbral plano `CV > 0.8` rechazaba goles de baja media; prefijos Bwin con sufijos (`FC`, `de Las Piedras`) fallaban; `coverage_summary` colapsaba a `0` si faltaba una familia auxiliar. | Límite `cv_limit = min(1.65, max(0.85, 1.35 / sqrt(max(0.25, min_mean))))`, matching robusto de alias locales/visitantes, y `basis = USABLE_FAMILIES_WITH_ALL_SERIES_AUDIT` separando `usable_series_count` del mínimo global. | [`test_prematch_tactical_signal_engine.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_prematch_tactical_signal_engine.py) + `test_fase_f_*`: **OK** |
| **F (Tests heredados)** | [`test_prematch_comparison_v21.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_prematch_comparison_v21.py#L17-L19) y [`test_prematch_research_profiles.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_prematch_research_profiles.py#L460-L467) | Pruebas heredadas que validaban exactamente los defectos previos: `basis == 'MINIMUM_ACROSS_PRODUCTION_AND_CONCESSION_SERIES'` y `1570018:home:goals -> UNSTABLE_PATTERN` por `CV > 0.80`. | Actualizadas para validar explícitamente `USABLE_FAMILIES_WITH_ALL_SERIES_AUDIT` (conservando `minimum_across_all_families_valid`) y `SUPPORTED_DESCRIPTIVE_SIGNAL` bajo `cv <= cv_limit` Poisson-aware en Granada–Las Palmas. | Incluidas en la corrida de 19 suites (`175` tests): **OK** |

---

## C. Estadística antes/después por mercado y familia

### 1. Walk-Forward estricto fuera de muestra (`1.753` bundles, `temporal_leakage_violations = 0`)

Comparación `Stage 1` (línea base) → `Stage 2` (regularización Bayesiana + Binomial Negativa / sobredispersión ajustada):

| Mercado / Familia | n obs | Brier (S1 → S2) | LogLoss (S1 → S2) | ECE (S1 → S2) | MAE (S1 → S2) | Cobertura recuento 80% (S1 → S2) | Bins SE ok (10 deciles) | Sesgo máx decil |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `goals > 0.5` | 40.446 | 0.19049 → **0.18675** | 0.56858 → **0.55898** | 0.04407 → **0.01974** | 1.0547 → **1.0376** | 0.921 → **0.926** | 10/10 | 0.0715 |
| `goals > 1.5` | 40.446 | 0.24182 → **0.23679** | 0.67920 → **0.66648** | 0.05429 → **0.01790** | 1.0547 → **1.0376** | 0.921 → **0.926** | 10/10 | 0.0520 |
| `goals_h1 > 0.5` | 3.628 | 0.25095 → **0.24358** | 0.69914 → **0.68070** | 0.06925 → **0.03870** | 0.6856 → **0.6809** | 0.933 → **0.937** | 10/10 | 0.0774 |
| `corners > 4.5` | 2.424 | 0.25391 → **0.24315** | 0.70549 → **0.67930** | 0.06669 → **0.02640** | 2.2667 → **2.2204** | 0.808 → **0.856** | 10/10 | 0.0705 |
| `shots > 11.5` | 2.374 | 0.22508 → **0.21551** | 0.64733 → **0.62037** | 0.05484 → **0.02443** | 4.3691 → **4.2889** | 0.728 → **0.824** | 10/10 | 0.0957 |
| `on_target > 3.5` | 2.424 | 0.22660 → **0.22047** | 0.64669 → **0.63143** | 0.04354 → **0.01088** | 2.0259 → **1.9916** | 0.819 → **0.852** | 10/10 | 0.0431 |
| `fouls > 11.5` | 2.259 | 0.23503 → **0.23252** | 0.66381 → **0.65685** | 0.04039 → **0.01188** | 3.1515 → **3.1307** | 0.819 → **0.841** | 10/10 | 0.0684 |
| `yellow > 1.5` | 2.421 | 0.23190 → **0.22767** | 0.65590 → **0.64703** | 0.04486 → **0.02163** | 1.1170 → **1.0910** | 0.919 → **0.931** | 10/10 | 0.0633 |

> **Alcance honesto de cobertura**: `count_interval_coverage_80` evalúa contención en el intervalo discreto de recuento de partidos futuros y `10bin_SE` verifica `RATE_BAND_BIN_CONTAINMENT_NOT_EQUATED_TO_FULL_CALIBRATION`; un decil contenido dentro de tolerancia estadística no demuestra rentabilidad financiera ni calibración perfecta condicional por liga menor. En `shots > 11.5`, el sesgo máximo de decil (`0.0957`) sigue siendo la mayor desviación residual.

### 2. Walk-Forward conjunto de partido (Dixon-Coles FT, `n = 9.954`)

| Mercado conjunto | Brier (S1 → S2) | LogLoss (S1 → S2) | ECE (S1 → S2) | Observación crítica |
|---|---:|---:|---:|---|
| `over_2_5` | 0.24186 → **0.23978** | 0.67656 → **0.67224** | 0.03919 → **0.01932** | Mejora clara en calibración y Brier. |
| `btts_yes` | 0.24602 → **0.24570** | 0.68518 → **0.68454** | 0.01483 → **0.02042** | **Ligero repunte de ECE (`+0.0056`)**: mejora LogLoss/Brier marginal pero empeora ECE empírico global. |
| `home_win` | 0.23811 → **0.23677** | 0.66849 → **0.66571** | 0.04804 → **0.03897** | Mejora Brier, Resolución (`0.00893 -> 0.00968`) y ECE. |

### 3. Holdout estricto de equipos no vistos (`dev = 1.305` vs `strict_holdout = 1.363`)

Se contrastó reducir el peso del prior (`k=3..5`) para explicar por qué `Wanderers` (`[6, 7, 4, 6]` remates propios) proyectaba `51.78%` antes de la corrección:
- En `corners > 4.5` (`n = 1.448` equipos no vistos): `k=8` logró **Brier = 0.24271**, frente a `k=5` (`0.24502`) y `k=4` (`0.24642`).
- En `shots > 11.5` (`n = 1.425` equipos no vistos): `k=8` logró **Brier = 0.21423** (mejor que `k=4` `0.21554` y `k=3` `0.21665`) y elevó `Count80%Cov` de `0.782` a `0.825` tras corregir `sample_mean` en NegBin, con un pequeño compromiso en ECE (`0.00606 -> 0.02047`).
- Conclusión metodológica: se conservó `shrink_k=8.0` para no sobreajustar a un partido uruguayo y se añadió la bandera explícita **`PRIOR_WEIGHT_EXCEEDS_LOCAL_SAMPLE`** cuando el prior domina la muestra local observada.

---

## D. Comparación de casos históricos congelados (Artefacto original vs Reevaluación offline)

> Los archivos originales en `outputs/matches/7909704/analysis/analisis_7909704.md` y `outputs/matches/7912816/analysis/analisis_7912816.md` **permanecieron sin modificar**. La columna «Después (reevaluación offline)» refleja el resultado al ejecutar los módulos corregidos sobre los mismos snapshots inmutables de entrada.

| Dimensión / Métrica | `7909704` (Estonia vs Bulgaria) — **Original congelado** | `7909704` — **Reevaluación offline actual** | `7912816` (Wanderers vs Juventud) — **Original congelado** | `7912816` — **Reevaluación offline actual** |
|---|---|---|---|---|
| **Selecciones cotizadas evaluadas** | 15 líneas cotizadas + 4 `SIN_CUOTA` (sobre 397 TXT) | **151 cotizadas** (+ 4 `BLOQUEO_TECNICO`), `155` comparaciones | 68/256 cotizadas (`26.56%`) + 4 `SIN_CUOTA` | **167/256 cotizadas** (`65.23%`), `171` comparaciones (+ 4 `BLOQUEO_TECNICO`) |
| **Taxonomía financiera** | Legacy + Shadow temprano (`15` EV bajo negativo; 0 picks) | `30` `CANDIDATO_CONDICIONAL`, `121` `NO_BET_POR_PRECIO`, `4` `BLOQUEO_TECNICO`, **0 `PICK_ACCIONABLE`** | `15` condicionales, `53` por precio, **0 picks** | `26` `CANDIDATO_CONDICIONAL`, `141` `NO_BET_POR_PRECIO`, `4` `BLOQUEO_TECNICO`, **0 `PICK_ACCIONABLE`** |
| **Equivalencias detectadas** | No auditadas (`exact_score_*` en `MERCADO_NO_IMPLEMENTADO`) | **5 grupos**, brecha máx **+9.52%** (`exact_score_1_1 @ 5.75` vs `btts_yes_and_under_2_5 @ 5.25`) y `+4.17%` (`0-0 @ 6.25` vs `under_0_5 @ 6.00`) | No auditadas (`exact_score_0_0 @ 11.50` excluido) | **5 grupos**, brecha máx **+9.52%** (`exact_score_0_0 @ 11.50` `+0.0431 EV` vs `under_0_5 @ 10.50` `-0.0476 EV`) y `+4.17%` (`1-1`) |
| **Resolución de entrenadores** | `J. Henn` / `A. Ribarski` (solapado con `I. Iliev`) | `J. Henn (CURRENT_VERIFIED)`, `A. Ribarski (CURRENT_VERIFIED)`, `regime_change=False` | Alertaba `Apud (785) / Tiscornia (791)` erróneamente en `contextual_evidence` | `Mathias Corujo (CURRENT_VERIFIED)`, `Ruben Blanco Soto Sergio (CURRENT_VERIFIED)`, `regime_change=False` |
| **Alertas contextuales / Copa** | Congestion / `LOW_CARD_REFEREE` (sobre `n=1` histórico arbitral) | `['FIXTURE_CONGESTION_ROTATION_RISK', 'LOW_CARD_REFEREE']` | Falsa alerta de congestión visitante por Copa `1649971` sin confirmar | `['HIGH_CARD_REFEREE', 'PROVISIONAL_CUP_LOOKAHEAD_UNCONFIRMED']` (congestión falsa suprimida) |
| **Ingesta del reporte cualitativo** | Identificadores dependían de cabecera manual | Compatible con formato completo | Requirió líneas manuales `Event ID:` / `Fixture ID:` y omitía horario CET / `11+11 PROBABLE` | Acepta formato original sin cabecera manual, extrae kickoff/cutoff UTC y `11+11 PROBABLE` |
| **Modelo `SHOTS > 11.5` y avisos** | `Estonia > 11.5` `55.35%` (`SIN_CUOTA`) | `home=59.99%`, `away=54.69%`, NegBin n=5 | Local `51.78%` pese a `[6, 7, 4, 6]` sin aviso explícito de dominio del prior | Local **`49.56%`** (NegBin `n=4` + `sample_mean` activo) con bandera **`PRIOR_WEIGHT_EXCEEDS_LOCAL_SAMPLE`**; `FOULS` marca `NO_OBSERVATIONS_USING_PRIOR` |
| **Score motor táctico PTSE** | Evaluado previamente (`3.4.1-pre-codex`) | Cobertura diferenciada FT/H1 y dueño resuelto | Bloqueado a **`0`** en ranking por prefijos largos + `CV > 0.8` + `coverage_summary=0` | Fixture `1649063`: **`74.69` (`EVALUATED`, 5 obs. válidas en familias útiles)** |

**Conclusión de negocio preservada**: ninguno de los dos partidos promociona artificialmente un pick (`0 PICK_ACCIONABLE`), pero en `7912816` se evita recomendar/omitir a ciegas entre mercados equivalentes (`0-0` a `11.50` frente a `Under 0.5` a `10.50`, ambos a probabilidad modelo idéntica `0.0907`), y desaparecen falsos vetos/alertas sobre entrenadores y congestión.

---

## E. Estado de pruebas tras la intervención

### 1. Suites activas de regresión y auditoría integral (corrida final tras última edición)

Comando ejecutado:
`PYTHONPATH=test:. .venv/bin/python -m unittest test_corrective_audit_v10 test_etapa2_contracts_and_store test_etapa3_analytics_core test_etapa4_probabilistic_and_markets test_etapa5_shadow_and_lifecycle test_etapa5_2_cli_shadow_integration test_market_coverage_and_taxonomy_stage1 test_probabilistic_audit_stage2 test_prematch_case test_prematch_case_resume_closure test_bwin_discovery_v31 test_discovery_policy_blocks test_discovery_fixture_audit_fixes test_prematch_tactical_signal_engine test_prematch_comparison_v21 test_prematch_research_profiles test_prematch_flexible_selection test_prematch_daily test_select_single_match -v`

- Resultado observado: **`Ran 175 tests in 13.770s — OK (skipped=1)`**.
- Única prueba omitida: `test_frozen_pasto_junior_identity_remains_exact` (`skipped 'frozen reconciliation artifact unavailable'`, artefacto histórico no versionado en el workspace).
- Verificación posterior adicional de Stage 2: `test_probabilistic_audit_stage2 -v` → **`Ran 4 tests in 0.510s — OK`**.

### 2. Situación del descubrimiento completo heredado (`python -m unittest discover -s test`)

En la corrida completa previa al ajuste de las dos aserciones heredadas (`test_prematch_comparison_v21` y `test_prematch_research_profiles`, ya resueltas arriba), `discover -s test` ejecutó `917` pruebas y registró `17` fallos (`15` preexistentes + aquellos `2` obsoletos), `37` errores y `17` omisiones por causas ajenas a esta auditoría y conservadas sin enmascarar:
- **~32 errores (`FileNotFoundError` / `ModuleNotFoundError`)** en archivos movidos históricamente a `test/` (`test_prematch_coverage_models`, `test_prematch_final_cycle`, `test_prematch_integral_analysis_v1046`, `test_prematch_reuse_odds_contract`, `test_prematch_prospective_trial`) que aún resuelven `ROOT = Path(__file__).resolve().parent` (apuntando a `test/` en lugar de la raíz) o referencian artefactos gitignored (`reconciliation_real_20261111`, `2026-11-02_madrid.json`).
- **12 fallos en `test_filter_adult_competitions`**: afirman la política antigua permisiva (`gender='UNKNOWN' -> ELIGIBLE`), sustituida por las políticas de bloqueo vigentes cubiertas en `test_discovery_policy_blocks` y `test_bwin_discovery_v31` (ambas `OK`).
- **1 fallo en `test_restored_mtime_cannot_refresh_cache`**: la caché endurecida añade `_acquired_at_utc` dentro del diccionario retornado.
- **1 fallo en `test_fase_a_b_recovery_quick_context`**: estado de replay devuelve `INSUFFICIENT_EVIDENCE` frente a `READY_FOR_MANUAL_RESEARCH` esperado por el fixture antiguo.

---

## F. Riesgos pendientes ordenados por criticidad

### P0 (Bloqueante para automatización ciega / apuestas reales autónomas)
- **Rentabilidad / EV económico en vivo sin demostrar**: las probabilidades Shadow tienen mejora verificada en Brier, LogLoss y ECE sobre el corpus offline, pero el diferencial frente a cierre Bwin real (`CLV`) y el rendimiento neto tras margen de casa siguen sin validación prospectiva suficiente para permitir apuestas automáticas (`0 PICK_ACCIONABLE` por defecto salvo validación estricta).

### P1 (Operativos relevantes, bajo vigilancia en la prueba controlada)
- **Sesgo de decil residual en `shots > 11.5` (`max_abs_bin_bias = 0.0957`) y `btts_yes` ECE (`0.01483 -> 0.02042`)**: exige cautela humana en props de remates de equipos cuando dispare `PRIOR_WEIGHT_EXCEEDS_LOCAL_SAMPLE`, y no sobreinterpretar ventajas estrechas (`< 3%` EV) en BTTS.
- **`LOW_CARD_REFEREE` sobre `n = 1` partido arbitral histórico** (observado en `7909704`): el plugin no impone todavía un suelo de muestra arbitral (`n >= 5`) antes de emitir etiqueta positiva/negativa de severidad del colegiado.
- **Pruebas legacy en `test/` con rutas relativas rotas (`ROOT = Path(__file__).resolve().parent`)**: aunque no afectan al runtime en producción, dificultan usar `unittest discover -s test` sin lista blanca de suites hasta migrar sus `ROOT` a `.parent.parent`.

### P2 (Menores / deuda de mantenimiento)
- **Mercados sin cobertura analítica directa restantes** (p. ej. jugadores individuales por falta de XI confirmados/minutos, córners por mitad, ganador desc/final 9 vías, remates/tarjetas jugador sin línea Bwin cotizada): permanecen clasificados explícitamente como `MERCADO_NO_IMPLEMENTADO` o `BLOQUEO_TECNICO` sin degradar el resto del catálogo.
- **Reconciliación manual cuando `report_generated_utc: null`**: el sistema protege el corte mediante `REPORT_COMPLETION_UNKNOWN` (`REVIEW`), manteniendo dependiente del operador el cierre temporal efectivo del consolidado Grok/Perplexity/Codex.

---

## G. Estado final

**`READY_FOR_CONTROLLED_PREMATCH_TRIAL`**

Justificación verificada:
1. Todas las rutas críticas PREMATCH (ingesta y validación de identidad de investigación cualitativa, resolución de entrenadores y alertas contextuales, modelado coherente FT/H1, auditoría de equivalencias Bwin, motor táctico PTSE y taxonomía de abstención) pasaron **175/175** comprobaciones ejecutadas en las **19 suites activas** (`1` skip por artefacto externo ausente).
2. La verificación Walk-Forward (`1.753` bundles, `0` fugas temporales) respalda la estabilidad numérica sin sobreajustar a `7912816` y detecta automáticamente cuándo el prior pesa más que la muestra local (`PRIOR_WEIGHT_EXCEEDS_LOCAL_SAMPLE`).
3. No se declara `READY_FOR_PRODUCTION` ni rentabilidad financiera demostrada: el ensayo próximo opera bajo supervisión humana, límite conservador (`CANDIDATO_CONDICIONAL` / `NO BET` por defecto cuando persistan bandas conservadoras negativas) y cierre previo al kickoff.

---

## H. Comandos exactos preparados para la ventana `17/11/2026 15:00 → 18/11/2026 03:00 CET` (SIN ejecutar)

> Verificar antes de ejecutar que el reloj del sistema esté en la ventana operativa y no repetir llamadas `--online` innecesarias si ya existe `handoff_prematch.json` vigente. Los placeholders `<EVENT_ID>`, `<FIXTURE_ID>` y `<EVAL_RUN_DIR>` se rellenan con la salida real del paso anterior.

### 1. Inspección previa de configuración sin red (`--dry-run`)
```bash
.venv/bin/python prematch_daily.py \
  --start 2026-11-17T15:00:00+01:00 \
  --end 2026-11-18T03:00:00+01:00 \
  --competition-scope adult-open \
  --dry-run
```

### 2. Discovery online + ranking + preflight Bwin (hasta investigación manual, nunca ejecuta `finish`)
```bash
.venv/bin/python prematch_daily.py \
  --rank --online --confirm-external \
  --start 2026-11-17T15:00:00+01:00 \
  --end 2026-11-18T03:00:00+01:00 \
  --competition-scope adult-open \
  --history 20 --details 8 \
  --select-verified --shadow-mode
```

*(Si se requiere reevaluar el handoff ya capturado antes de elegir manualmente sin gastar cuotas nuevas:)*
```bash
.venv/bin/python prematch_daily.py \
  --review outputs/discovery/2026-11-17_1500_0300/evaluations/<EVAL_RUN_DIR>/prospective_trial/handoff_prematch.json \
  --review-min-lead 45
```

### 3. Preparación canónica del partido seleccionado y adjunto de investigación cualitativa (Grok + Perplexity)
```bash
.venv/bin/python prematch_case.py prepare \
  --handoff outputs/discovery/2026-11-17_1500_0300/evaluations/<EVAL_RUN_DIR>/prospective_trial/handoff_prematch.json
```
```bash
.venv/bin/python prematch_case.py attach-research \
  --event-id <EVENT_ID> \
  --file outputs/matches/<EVENT_ID>/inputs/reporte_cualitativo_<EVENT_ID>.md
```

### 4. Comprobación de estado, adquisición tardía de alineaciones / refresco final de cuotas (entre T-45 y T-5)
```bash
.venv/bin/python prematch_case.py status --event-id <EVENT_ID>
```
```bash
.venv/bin/python prematch_case.py acquire-lineups \
  --selected outputs/matches/<EVENT_ID>/selection/<TIMESTAMP>/selected_match.json
```
```bash
.venv/bin/python prematch_case.py refresh-odds \
  --selected outputs/matches/<EVENT_ID>/selection/<TIMESTAMP>/selected_match.json
```

### 5. Cierre PREMATCH final en Shadow Mode (una sola vez con XI/cuotas finales verificadas)
```bash
.venv/bin/python prematch_case.py finalize \
  --selected outputs/matches/<EVENT_ID>/selection/<TIMESTAMP>/selected_match.json \
  --shadow-mode
```
