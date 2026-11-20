# Diagnóstico Cuantitativo del Motor Probabilístico — Iteración 2.0

**Proyecto:** `bet-tactical-engine`  
**Rama:** `refactor/etapa2-fundamentos`  
**Commit base (HEAD):** `7390981d8da690e34eb96a1dbf944d9e67546708`  
**Alcance:** Auditoría reproducible de flujo matemático, profundidad histórica, backtest Walk-Forward as-of y sensibilidad `7909713` (sin cambios al modelo en producción).

---

## 1. Recorrido completo del flujo matemático actual

**Cadena auditada:**  
`API-Football -> generate_context.py -> ObservationStore -> selección histórica -> ponderaciones -> priors -> lambdas -> distribuciones -> probabilidades -> calibración -> BwinMarketEvaluator`

| Etapa | Función / Clase y Archivo | Fórmula / Lógica actual | Parámetros actuales | Fuente de datos y tamaño real | Limitaciones / Sesgos detectados |
|---|---|---|---|---|---|
| 1. Captura histórica API-Football | `resolve_deep_history_limit` (`config/constants.py:75-89`), `ContextHistoryCollector.collect_team_history` (`generate_context.py:2257-2340`), `ContextGenerator` (`generate_context.py:2735-2790`) | `eff_limit = max(1, min(20, int(limit)))`; consulta `/fixtures?team={id}&last={eff_limit}` y `/fixtures?ids=...` en lotes de `FIXTURE_IDS_BATCH_SIZE=20`, filtrando `COMPLETED_STATUSES={"FT","AET","PEN"}` y `historical_available(it, cutoff)`. | `MICRO_HISTORY_LIMIT = 6`, `DEEP_HISTORY_LIMIT = 6` (`config/constants.py:67-68`), `LAST_MATCHES_LIMIT = 10`, `RECENT_FIXTURES_LIMIT = 20`. | `match_engine_*.json` recibe por defecto **6** partidos por equipo (`COBERTURA_HISTORICA.csv`: 10/10 bundles de `outputs/matches/` tienen medianas/rangos 4–6); `preselection.json` captura hasta **20** goles y estadísticas detalladas parciales. | Cuello de botella estructural en `config/constants.py:68`: aunque el modelador admite `history_limit=20` y la puerta accionable exige `sample_n >= 10`, `generate_context.py` recorta el paquete PREMATCH estándar a 6 partidos. |
| 2. Ingesta e higiene as-of | `ObservationStore.from_match_engine_json` (`bet_engine/context/observation_store.py:502-680`) y `from_preselection_match` (`observation_store.py:924+`) | Deduplica por `fixture_id`, exige `status in {"FT","AET","PEN"}`, valida `available_at_utc <= cutoff_utc` y `kickoff_utc < cutoff_utc`, marca `red_card_distortion` con `_analyze_red_card_events`, ordena descendente por `kickoff_utc` y recorta `history_limit`. | Sin recorte propio adicional (`history_limit=20` desde modelador; en `preselection` hasta 20). | Corpus `audit/iteracion_2_0/COBERTURA_HISTORICA.csv` (888 filas: 10 `match_engine`, 878 `preselection`). En `preselection` (32,373 filas equipo-partido): goles `100.0%`, corners `10.2%`, on_target `10.2%`, yellow `10.2%`, shots `10.0%`, fouls `9.3%`, goals_h1 `2.9%`. En filas únicas equipo-partido (`29,571`): `goals=29,571`, `yellow=3,175`, `on_target=3,170`, `corners=3,168`, `shots=3,119`, `fouls=2,884`, `goals_h1=728`. | Cobertura estadística no-goles muy dispersa en `preselection` (máximo 11 partidos detallados por equipo en el corpus; solo `440/1,589` equipos tienen >=3 partidos con córners). |
| 3. Exclusión rojas y deduplicación H2H | `_red_card_weight` (`bet_engine/modeling/distributions.py:372-390`), `CoherentMatchModeler.build_model` (`distributions.py:595-858`) | `_red_card_weight`: con política `"exclude_any"`, devuelve `0.0` si `red_card_distortion` es `True`. En varianza conjunta H2H compartidos se excluyen para evitar doble conteo (`SHARED_H2H_DEDUPLICATED`). Por otro lado, `opp_sel` aplica descuento `0.50` a partidos compartidos H2H (`distributions.py`). | Política fija `exclude_any`; descuento H2H `0.50`. | Muestras por equipo según disponibilidad post-filtro. En `7909713` (Argentina vs Costa Rica), `match_engine_7909713.json` aporta 6 partidos por bando (`ARG`: 4 en torneo + 2 amistosos/otros; `CR`: 4+2). | Descartar `red_card_distortion=True` con peso `0.0` reduce aún más el tamaño muestral efectivo cuando `n=6`. |
| 4. Selección de muestra y ponderación por competición | `_estimate_team_rate_detailed` (`bet_engine/modeling/distributions.py:393-564`) | Si partidos de misma `(league_id, season) >= 4` (después de filtrar rojas y nulos), solo usa esa competición con peso `1.0`; si `< 4`, activa fallback mixto (`COMPETITION_MIX_FALLBACK`) con pesos: `1.0` misma liga+temporada, `0.50` mismo `competition_stratum`, `0.25` distinto estrato. Mezcla ventana reciente L6/L20: si `len(pairs) >= 7`, `est = 0.35 * mean_w(L6) + 0.65 * mean_w(L20)`; si `< 7`, usa `L20`. | `history_limit=20`, umbral de liga única `>=4`, umbral L6/L20 `>=7`, pesos `(1.0, 0.50, 0.25)`, mezcla `(0.35, 0.65)`. | Real en `match_engine`: <=6 partidos por equipo (por lo que `len(pairs) < 7` y la mezcla L6/L20 no se activa). En `preselection`: 790/878 (`90.0%`) tienen `>=10` en ambos equipos y 408/878 tienen `>=20` en ambos. | 1) **Ausencia total de ponderación por fuerza del rival** (`opponent strength` = nulo en producción). 2) **Ausencia de decaimiento exponencial por tiempo** (salvo escalón discreto L6/L20) y **sin separación por régimen de entrenador**. 3) En selecciones nacionales con mezcla clasificatorio/amistoso, el peso `0.50` o `0.25` reduce `n_eff` pero comprime hacia un prior único. |
| 5. Priors por estrato y contracción Empirical Bayes | `DEFAULT_PRIORS`, `STRATUM_PRIORS`, `_resolve_prior`, `_estimate_team_rate_detailed` (`distributions.py:332-363, 393-564`) | Contracción hacia `prior_mean`: `shrunk = (n_eff * est + shrink_k * prior_mean) / (n_eff + shrink_k)`. Si hay `>=3` partidos del mismo `venue` (`HOME`/`AWAY`), mezcla `0.75 * shrunk + 0.25 * mean(venue)`. Dispersión: `shrunk_phi = (n_eff * raw_phi + 12.0 * phi_prior) / (n_eff + 12.0)`. | `shrink_k = 8.0`, `phi_k = 12.0`, `venue_blend = 0.25`. `DEFAULT_PRIORS["goals"] = (1.56, 1.26)`; `STRATUM_PRIORS["NATIONAL_TEAM"]["goals"] = (1.50, 1.40)` (único estrato diferenciado). `FAMILY_DISPERSION_FLOORS["goals"] = 1.18`. | Aplicado sistemáticamente en las 7 familias (`goals`, `goals_h1`, `corners`, `shots`, `on_target`, `fouls`, `yellow`). | Con `n=6` y pesos de competición mixtos (`n_eff ≈ 4.0–5.0`), `shrink_k=8.0` otorga **61.5%–66.7% del peso total al prior genérico**. Además, `STRATUM_PRIORS["NATIONAL_TEAM"]` trata igual a Argentina que a un rival de ranking inferior (`(1.50, 1.40)`). |
| 6. Combinación ofensiva/defensiva (`lambda`) e incertidumbre | `_estimate_team_rate_detailed`, `_gamma_rate_bounds`, `CoherentMatchModeler.build_model` (`distributions.py:126-136, 540-564, 595-858`) | **Combinación aditiva lineal:** `lam = max(0.05, 0.55 * own_shrunk + 0.45 * opp_shrunk)`. Bandas Gamma Wilson-Hilferty: `_gamma_rate_bounds(lam, n_post, phi)` con `z = 1.2816` (`alpha = max(1.5, n_safe * lam / phi)`), donde `n_post = (n_eff + 8.0) * (0.85 si COMPETITION_MIX_FALLBACK else 1.0)`. Parámetro `accepted_features` aceptado y validado en firma, pero **no utilizado** en el cálculo de `build_model`. | Pesos `(0.55, 0.45)`, `z = 1.2816` (banda ~80% por lado), penalización mixta `0.85`. | Idem anterior; `exploratory_artifacts` son ignorados (`ignored_exploratory_count`). | **Combinación aditiva en lugar de multiplicativa / log-lineal**: comprime extremos (subestima favoritos y sobrestima débiles). Además, la propagación por esquinas `(lh_lo/hi, la_lo/hi)` produce intervalos `[p_lo, p_hi]` de ancho promedio **~28–36 puntos porcentuales**, haciendo casi imposible `ev_lo > 0` en mercados con margen normal. |
| 7. Distribuciones de probabilidad y mercados | `dixon_coles_joint_grid` (`distributions.py:88-123`), `build_count_pmf` (`57-73`), `convolve_pmfs` (`76-85`) | **Goles FT:** matriz bivariada Poisson `0..12` con ajuste Dixon-Coles `tau(i,j)` en marcadores bajos `(0,0),(0,1),(1,0),(1,1)` (`rho = -0.04`). **Resto de familias (`goals_h1`, `corners`, `shots`, `on_target`, `fouls`, `yellow`):** `build_count_pmf` elige Binomial Negativa si `n_obs >= 4` y `var > 1.15 * lam` (`r = max(1.5, min(80.0, lam^2 / max(0.05, var - lam)))`), si no Poisson; el total es convolución independiente `convolve_pmfs(home_pmf, away_pmf)`. | `rho = -0.04`, `max_goals = 12`, umbral NegBin `1.15 * lam` con `n_obs >= 4`. | Coherencia 1X2 = 1.0 y BTTS/Totales derivados de la misma matriz en `GOALS_FT`. | En `corners`, `shots`, `fouls`, `yellow`, la convolución independiente asume correlación 0 entre equipos, y al ser `n_obs` de estadísticas detalladas frecuentemente `< 4`, cae en Poisson puro subestimando la varianza de totales. |
| 8. Calibración y evaluación Bwin | `WalkForwardEvaluator` (`bet_engine/modeling/calibration.py:359+`), `_family_calibration_info`, `_classify_option`, `BwinMarketEvaluator.evaluate_standard_markets` (`bet_engine/markets/bwin_evaluator.py:124-380`), `ShadowPipelineRunner` (`bet_engine/orchestration/shadow_pipeline.py:567-575`) | `_classify_option` exige conjuntamente: `lineups_confirmed_complete`, `calibration_accredited`, `ev_lo = odds * p_lo - 1 > 0.0`, `sample_n >= 10` (sin `SMALL_SAMPLE_EXPLORATORY_ONLY`), sin `historical_zero_risk` ni `high_tail_sensitivity` (`p_mod >= 0.05`). `BwinMarketEvaluator` recibe `calibration_accredited_families: frozenset() = frozenset()` por defecto; `ShadowPipelineRunner` **no pasa nunca ese argumento**. | `sample_n >= 10`, `ev_lo > 0.0`, `p_mod >= 0.05`. `calibration_accredited_families = frozenset()` en producción. | `evaluate_standard_markets` en Shadow. | Bloqueo estructural doble: aunque un partido superara `sample_n >= 10` y `ev_lo > 0`, **ninguna familia puede ser `CALIBRATED_OUT_OF_SAMPLE`** porque ni existe artefacto de calibración cargado ni `ShadowPipelineRunner` conecta `calibration.py` con `BwinMarketEvaluator`. |

---

## 2. Cobertura histórica real (`audit/iteracion_2_0/COBERTURA_HISTORICA.csv`)

Auditoria sobre **888** expedientes locales (`10` `match_engine_*.json` en `outputs/matches/` y `878` bundles únicos de `preselection`):

1. **`match_engine` (`n = 10` partidos completos en `outputs/matches/`):**
   - `config/constants.py:68` fija `DEEP_HISTORY_LIMIT = MICRO_HISTORY_LIMIT = 6`.
   - Resultado observado: los snapshots operativos de partidos analizados (`7909713`, `7909714`, `7905487`, etc.) solo contienen **4–6 partidos** en `home_history_deep`/`away_history_deep`, por debajo de `sample_n >= 10`.
2. **`preselection` (`n = 878` bundles únicos):**
   - El pipeline de preselección ya captura hasta 20 partidos de goles por equipo: **`790 / 878` (`90.0%`)** disponen de `>= 10` partidos en ambos equipos y **`408 / 878`** disponen de `>= 20` en ambos equipos.
   - Sin embargo, sobre las **32,373** filas equipo-partido de `preselection` (`29,571` pares únicos `(team_id, fixture_id)`):
     - `goals`: `100.0%` (`29,571` únicos)
     - `yellow`: `10.2%` (`3,175` únicos)
     - `on_target`: `10.2%` (`3,170` únicos)
     - `corners`: `10.2%` (`3,168` únicos)
     - `shots`: `10.0%` (`3,119` únicos)
     - `fouls`: `9.3%` (`2,884` únicos)
     - `goals_h1`: `2.9%` (`728` únicos)
   - En el corpus reconstruido, solo **`440 / 1,589`** equipos alcanzan `>= 3` partidos históricos con estadísticas detalladas (`corners`), con un máximo de `11` por equipo.
3. **Disponibilidad de fuerza del rival as-of sin peticiones externas:**
   - Para la mitad de prueba del backtest Walk-Forward (`2,248` partidos conjuntos en ventana L20, `69,289` apariciones de rivales en sus historiales), **`35,713 / 69,289` (`51.5%`)** de esos rivales ya tienen `>= 3` partidos previos propios registrados as-of dentro del propio corpus de `preselection`.

---

## 3. Resultados del Backtest Walk-Forward as-of (`audit/iteracion_2_0/BASELINE_BACKTEST.json`)

Protocolo: partición estrictamente temporal pre-kickoff (`available_at_utc <= kickoff_utc`), sin `red_card_distortion`, excluyendo `18,567` registros con `< 6` partidos previos en algún bando, quedando **`4,496` partidos conjuntos elegibles**:
- **Train (tasas base climatológicas por estrato):** `2,248` partidos (`2025-10-11T18:00:00+00:00` a `2026-09-26T14:00:00+00:00`). Medias observadas `(home, away)`: `ALL = (1.6299, 1.3470)`, `CLUB = (1.6290, 1.3479)`, `NATIONAL_TEAM = (1.6857, 1.2857)`.
- **Test fuera de muestra:** `2,248` partidos (`2026-09-26T14:00:00+00:00` a `2026-11-14T19:45:00+00:00`; `2,197` de clubes + `51` de selecciones nacionales).

### 3.1. Comparativa `L6` vs `L10` vs `L20` vs Referencia simple (`ALL`, `n = 2,248`)

| Modelo / Ventana | 1X2 Brier (por salida) | 1X2 Log-Loss | 1X2 RPS | HomeWin ECE (5 bins) | Over 2.5 Brier / ECE | BTTS Yes Brier / ECE | MAE Goles Total | RMSE Goles Total | Sesgo Goles Home (`pred-obs`) | Sesgo Goles Away (`pred-obs`) |
|---|---|---|---|---|---|---|---|---|---|---|
| `L6` (actual en `match_engine`) | `0.20707` | `1.03336` | `0.22217` | `0.05689` | `0.23925` / `0.04328` | `0.24565` / `0.01700` | `1.4184` | `1.7770` | `-0.1464` | `-0.0136` |
| `L10` | `0.20522` | `1.02578` | `0.21950` | `0.05549` | `0.23714` / `0.03526` | `0.24495` / `0.01279` | `1.4097` | `1.7640` | `-0.1472` | `+0.0028` |
| `L20` (tope del modelador) | **`0.20428`** | **`1.02191`** | **`0.21811`** | **`0.05568`** | **`0.23645`** / `0.03633` | **`0.24417`** / `0.01576` | **`1.4093`** | **`1.7633`** | **`-0.1650`** | **`+0.0125`** |
| `simple_base_rate_reference` | `0.21315` | `1.05859` | `0.23087` | `—` | `—` | `—` | `1.4164` | `1.8091` | `-0.0748` | `-0.0165` |

### 3.2. Sesgo sistemático en favoritos vs underdogs y selecciones nacionales

1. **Compresión de favoritos en `L20 ALL` (sobre eventos 1X2):**
   - `favorite_ge_0_55` (`n = 131`): probabilidad media predicha `0.5949` vs tasa empírica observada **`0.7328`** -> sesgo **`-0.1379`** (`-13.79 pp`, subestimación severa de favoritos). En el bin `home_win [0.6, 0.8]` (`n = 31`), predijo `0.6439` frente a `0.8387` observado (`-19.48 pp`).
   - `balanced_0_35_to_0_55` (`n = 2,512`): predicha `0.4237` vs observada `0.4701` (sesgo `-0.0464`).
   - `underdog_lt_0_35` (`n = 1,853`): predicha `0.2901` vs observada **`0.2445`** -> sesgo **`+0.0456`** (sobrestimación sistemática de no-favoritos).
   - **Subestimación del factor campo (`bias_home`):** `L20 ALL` subestima goles del local en `-0.1650` goles/partido (`obs_rate(home_win) = 0.4644` vs `pred_mean = 0.4096`).
2. **Estrato `NATIONAL_TEAM` (`n = 51` en test):**
   - `L20`: `1X2 Brier = 0.19370`, `Log-Loss = 0.97842`, `RPS = 0.20615` (vs base-rate `0.21866` / `1.08057` / `0.24289`). En los `6` casos con `p >= 0.55`, predijo `0.5742` y ganaron `1.0000` (`6/6`, sesgo `-0.4258`), confirmando en pequeña muestra el mismo patrón compresor.
3. **Emparejamiento Bwin finalizado (`bwin_matched_finished_fixtures`):**
   - Solo **`6`** partidos finalizados con cuotas de cierre en `bwin_discovery.json` (`fixtures 1528973..1528978`). Se clasifica explícitamente como **`MUESTRA_INSUFICIENTE_PARA_INFERENCIA`**; no permite inferencia probabilística ni comparar calibración contra Bwin fuera de muestra.
4. **Cobertura de intervalos (`evaluate_joint_match_walk_forward`, `n = 4,411`) y métricas no-goles (`WalkForwardEvaluator`):**
   - En `evaluate_joint_match_walk_forward` (`n = 4,411`), los intervalos `[p_lo, p_hi]` muestran `raw_bin_coverage = 1.0 (5/5)` en `over_2_5`, `btts_yes` y `home_win`, **pero** a costa de anchos promedio de **`0.3561`**, **`0.2840`** y **`0.3505`** respectivamente (35 pp de amplitud media por propagar esquinas Gamma con `z = 1.2816` y penalización `n_eff`).
   - En evaluación por equipo individual (`goals > 1.5`, `min_prior = 10`, `n = 14,038`): `W10` mejora significativamente a `W6` (`Brier 0.238953` vs `0.240636`, `delta = -0.001683`, bootstrap `prob_alt_improves = 1.0`). En estadísticas detalladas (`min_prior = 3`), solo `fouls > 11.5` (`n = 1,611`, `W6` vs `W3`, `prob_alt_improves = 0.992`) muestra mejora clara con la densidad muestral actual; `corners` (`n = 1,747`), `shots` (`n = 1,704`), `on_target` (`n = 1,748`), `yellow` (`n = 1,751`) y `goals_h1` (`n = 283`) quedan limitados por escasez de detalle.

---

## 4. Sensibilidad de Argentina vs Costa Rica (`7909713`) — `audit/iteracion_2_0/SENSIBILIDAD_7909713.json`

Reproducción exacta del snapshot `match_engine_7909713.json`:  
- **Baseline producción (`n_home=6, n_away=6`):** `lambda_home = 2.1916`, `lambda_away = 1.0162`, `P(home_win) = 0.6372`, `P(draw) = 0.2022`, `P(away_win) = 0.1606`, `P(Costa Rica >= 1 gol) = 0.6380`, `P(Over 2.5) = 0.6217`.  
- **Participación real del prior genérico `NATIONAL_TEAM` `(1.50, 1.40)`:**
  - Ataque Argentina (`w_eff = 5.0`): peso del prior **`61.54%`** (`8 / 13`).
  - Defensa Costa Rica (`w_eff = 4.0`): peso del prior **`66.67%`** (`8 / 12`).
  - Ataque Costa Rica (`w_eff = 4.0`): peso del prior **`66.67%`**.
  - Defensa Argentina (`w_eff = 5.0`): peso del prior **`61.54%`**.

**Descomposición cuantitativa OAT (One-At-A-Time) sobre `7909713`:**

| Perturbación controlada | `lambda_home` | `lambda_away` | `P(1)` | `P(X)` | `P(2)` | `P(CR >= 1)` | Lectura |
|---|---|---|---|---|---|---|---|
| `baseline_production` | `2.1916` | `1.0162` | `0.6372` | `0.2022` | `0.1606` | `0.6380` | Caso observado en Shadow |
| `shrink_k_0` (sin contracción a prior) | `2.7356` | `0.7400` | **`0.7888`** | `0.1384` | `0.0727` | `0.5229` | **`+15.16 pp`** en `P(1)` solo por eliminar `shrink_k=8` |
| `multiplicative_attack_defense` | `2.9784` | `0.7250` | **`0.8222`** | `0.1187` | `0.0591` | `0.5157` | **`+18.50 pp`** por sustituir `0.55*own+0.45*opp` por interacción multiplicativa |
| `exploratory_asymmetric_prior_2_2_vs_0_7` | `2.5268` | `0.6783` | `0.7725` | `0.1517` | `0.0758` | `0.4925` | `+13.53 pp` con prior asimétrico |
| `exploratory_sos_discount_cr_0_65_arg_1_20` | `2.3963` | `0.9440` | `0.6919` | `0.1805` | `0.1276` | `0.6109` | `+5.47 pp` por ajuste exploratorio SoS |
| `shrink_k_4` | `2.3694` | `0.9030` | `0.7037` | `0.1774` | `0.1189` | `0.5946` | `+6.65 pp` |
| `same_stratum_weight_1_0` | `2.2121` | `1.0014` | `0.6462` | `0.1986` | `0.1552` | `0.6326` | Efecto menor (`+0.90 pp`) |
| `dixon_coles_rho_minus_0_08` / `rho_0` | `2.1916` | `1.0162` | `0.6336` / `0.6408` | `0.2094` / `0.1950` | `0.1570` / `0.1642` | `0.6380` | Efecto marginal (`±0.36 pp`) |

**Conclusión de sensibilidad `7909713`:** La brecha entre el `63.72%` de Shadow y escenarios más asimétricos (~`79%–82%`) no se debe a `rho` de Dixon-Coles ni al peso entre torneos del mismo estrato, sino a la combinación de: (1) **`shrink_k=8` sobre `n=6`** (que fuerza >61% de masa hacia un prior único `(1.50, 1.40)`), (2) **combinación aditiva `0.55*own + 0.45*opp`** en lugar de interacción multiplicativa ataque×defensa, y (3) **ausencia de ajuste por calidad de rivales**. No obstante, tampoco en `7909713` puede inferirse un `94.79%` desde `n=6` sin datos externos o ajuste estructural de ranking/Elo de selecciones.

---

## 5. Defectos matemáticos y estructurales demostrados

1. **Defecto D1 — Contracción excesiva con muestra truncada (`DEEP_HISTORY_LIMIT=6`, `shrink_k=8.0`):** produce pesos del prior >60% en snapshots operativos y explica directamente el sesgo de compresión (`-13.79 pp` en favoritos `>=0.55`, `+4.56 pp` en no-favoritos `<0.35`).
2. **Defecto D2 — Combinación aditiva `0.55*own + 0.45*opp`:** amortigua la interacción no lineal ataque fuerte vs defensa débil (y viceversa), agravando el sesgo en partidos desbalanceados y subestimando `lambda_home` (`-0.165` goles/partido en `L20 ALL`).
3. **Defecto D3 — Ausencia de ajuste por fuerza de rival (SoS / Elo):** el estimador en producción pondera todos los rivales igual, aunque en `preselection` (`51.5%` de apariciones en L20) ya existe información histórica as-of de los rivales. `_estimate_experimental_goal_rate` en `calibration.py:1079-1205` demuestra que el diseño offline ya contemplaba `sos_alpha`, `half_life_days`, `coach_decay_factor` y modo multiplicativo, sin haber pasado aún a producción.
4. **Defecto D4 — Intervalos excesivamente anchos (`~35 pp`) y puerta de calibración desconectada:** `[p_lo, p_hi]` propagado por esquinas `z=1.2816` y `calibration_accredited_families=frozenset()` impiden distinguir calibración real por familia en producción.
5. **Defecto D5 — Subutilización de `AcceptedModelFeature` (`accepted_features`) y cobertura escasa en no-goles:** `build_model` valida tipo de `accepted_features`, pero ignora su contenido; y las métricas detalladas (`corners`, `shots`, `on_target`, `fouls`, `yellow`, `goals_h1`) solo cubren ~10% (`~3%` en H1) de los partidos en `preselection`.
