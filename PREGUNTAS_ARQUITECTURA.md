# PREGUNTAS ARQUITECTÓNICAS CRÍTICAS PARA EL USUARIO (ETAPA 1)

> **Instrucciones**: A continuación se presentan **12 decisiones arquitectónicas de alto impacto** surgidas directamente de la auditoría del código real. Ninguna es cosmética: cada respuesta define cómo se unificarán los motores, cómo fluirán los datos entre etapas y qué reglas estadísticas gobernarán el nuevo núcleo analítico.

---

## PREGUNTA 1: Unificación de los dos pipelines diarios (`preselect_matches` vs. `prematch_phase1/phase2`)

Actualmente [prematch_daily.py](file:///Users/antonio/Projects/bet-tactical-engine/prematch_daily.py) alberga dos pipelines diarios incompatibles: el modo por defecto (`prematch_prospective_trial.py` $\to$ `preselect_matches.py` $\to$ `prematch_pattern_discovery.py`) y el modo controlado (`prematch_execution_control.py` $\to$ `prematch_phase1.py` $\to$ `prematch_phase2.py` $\to$ `prematch_advanced_discovery_v2.py`).

- **Decisión arquitectónica afectada**: Diseño de la Capa A (Adquisición), Capa B (`ObservationStore`) y Capa F (`PipelineRunner` diario).
- **Por qué existen varias alternativas**:
  - *Alternativa 1A (Unificación en dos fases bajo un único `ObservationStore`)*: Mantener un barrido ligero Fase 1 (`history=20`, `details=8..10`) para todos los partidos del día y ejecutar automáticamente Fase 2 (jugadores, alineaciones esperadas, minería profunda `advanced_discovery_v2` y modelos predictivos) solo sobre los Top $K$ fixtures preseleccionados, usando el mismo cliente HTTP/caché y el mismo formato `ObservedFact`.
  - *Alternativa 1B (Mantener ambos flujos separados)*: Conservar `preselect_matches.py` para el escaneo rápido y `prematch_phase1.py` como flujo paralelo independiente.
- **Mi recomendación como arquitecto**: **Alternativa 1A**. Unificar `preselect_matches.py` y `prematch_phase1.py` bajo el cliente y caché versionada de `Phase1ApiClient` y un único `ObservationStore`.
- **Consecuencias de cada respuesta**:
  - Si eliges **1A**: Eliminamos la duplicación de llamadas HTTP, desaparece el adaptador parche `adapt_preselection_to_phase1_payload` ([prematch_daily.py:628](file:///Users/antonio/Projects/bet-tactical-engine/prematch_daily.py#L628)) y los Top candidatos del día reciben siempre análisis de jugadores (`phase2`), minería de interacciones y modelos probabilísticos.
  - Si eliges **1B**: Seguiremos manteniendo dos esquemas de JSON distintos (`preselection.json` vs `fixtures_context.json`) y dos cachés que pueden desincronizarse.

---

## PREGUNTA 2: Eliminación del embudo `P-1..P-6` en `generate_context.py` y `motor_probabilistico.py`

Al pasar un partido a `prematch_single.py finish` o `prematch_multi.py finish`, [`generate_context.py`](file:///Users/antonio/Projects/bet-tactical-engine/generate_context.py) recorta la micro-historia impresa a `MICRO_HISTORY_LIMIT = 6` y [`motor_probabilistico.py:271`](file:///Users/antonio/Projects/bet-tactical-engine/motor_probabilistico.py#L271) parsea el `.txt` por regex ignorando cualquier partido $> 6$.

- **Decisión arquitectónica afectada**: Contrato de entrada de `motor_probabilistico.py` (Capa D/E) y persistencia del `ObservationStore` entre la fase diaria y el cierre del partido.
- **Por qué existen varias alternativas**:
  - *Alternativa 2A (Consumo directo del JSON estructurado `N=20` + vistas multi-ventana)*: Hacer que `motor_probabilistico.py` lea directamente el JSON estructurado del `ObservationStore` con los **20 partidos históricos** (y hasta 10–15 detallados), reportando en paralelo las ventanas `L20_season`, `L10_recent`, `L6_micro`, `same_competition` y `venue_specific`, mientras `match_engine_<event>.txt` mantiene el resumen legible para humanos.
  - *Alternativa 2B (Subir `MICRO_HISTORY_LIMIT` en el `.txt` y seguir parseando texto por regex)*: Aumentar el límite de 6 a 12 o 15 en el `.txt`.
  - *Alternativa 2C (Mantener `P-1..P-6` como única fuente del motor probabilístico)*.
- **Mi recomendación como arquitecto**: **Alternativa 2A**. Un motor estadístico nunca debe parsear su propia salida de texto por regex cuando tiene el JSON estructurado en disco.
- **Consecuencias de cada respuesta**:
  - Si eliges **2A**: Ganamos potencia estadística real ($N=15\text{–}20$ en vez de $N \le 6$), permitimos splits por localía con muestra suficiente ($n \ge 7\text{–}10$ en casa/fuera) y conectamos los patrones descubiertos en la fase diaria directamente con la evaluación de mercados en `finish`.
  - Si eliges **2B**: El `.txt` crecerá enormemente en tamaño y el parser por regex seguirá siendo frágil ante cualquier cambio de formato.
  - Si eliges **2C**: Cualquier split por localía o competición en `motor_probabilistico.py` seguirá teniendo muestras de $n = 2\text{–}3$ partidos, impidiendo validar patrones con rigor.

---

## PREGUNTA 3: Destino de la Isla Analítica Profunda `v1042–v1050` (SoS, H1/H2, Rojas, Arquetipos)

Los 11 módulos de la serie `v1042–v1050` ([prematch_sos_v1044.py](file:///Users/antonio/Projects/bet-tactical-engine/prematch_sos_v1044.py), [prematch_integral_analysis_v1046.py](file:///Users/antonio/Projects/bet-tactical-engine/prematch_integral_analysis_v1046.py), [prematch_tactical_intelligence_v1050.py](file:///Users/antonio/Projects/bet-tactical-engine/prematch_tactical_intelligence_v1050.py), etc.) contienen lógica táctica muy superior a la del motor diario (ajuste por fuerza del rival SoS, distorsión por tarjeta roja $<60'$, arquetipos tácticos, dinámicas H1 vs H2), pero hoy están **100% desconectados** del flujo productivo.

- **Decisión arquitectónica afectada**: Composición de `bet_engine.context` (Capa B) y `bet_engine.analytics` (Capa C2), y deprecación futura de scripts aislados.
- **Por qué existen varias alternativas**:
  - *Alternativa 3A (Integración como plugins de primera clase en el nuevo núcleo)*: Extraer la lógica matemática/táctica de `v1044` (SoS), `v1046` (rojas $<60'$, H1/H2, bajas) y `v1050` (arquetipos y choques tácticos) e integrarla dentro del `ObservationStore` y el `PatternRegistry` para que alimente tanto el ranking diario como el dossier de Codex.
  - *Alternativa 3B (Mantenerlos como herramienta manual separada por línea de comandos)*.
- **Mi recomendación como arquitecto**: **Alternativa 3A**. Es el activo táctico más rico que ya has construido en el repositorio; dejarlo muerto fuera del flujo principal sería desperdiciar meses de diseño.
- **Consecuencias de cada respuesta**:
  - Si eliges **3A**: Todas las señales de córners, remates, goles y tarjetas pasarán a estar ajustadas por la calidad de los rivales enfrentados (SoS) y limpias de distorsiones por expulsiones tempranas, sin coste adicional de API cuando se reutilice la caché de historia de la liga.
  - Si eliges **3B**: El núcleo principal seguirá tratando igual un partido contra el líder que contra el colista y seguirá contaminando medias con partidos jugados 70 minutos en inferioridad numérica.

---

## PREGUNTA 4: Estrategia ante muestras cortas en Competiciones Europeas, Copas e Inicio de Temporada

Hoy [`prematch_pattern_discovery.py:83`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_pattern_discovery.py#L83) (`clean_observations`) exige coincidencia exacta de `league_id` y `season`. En Champions League, Europa League, Copas nacionales o en las primeras 6 jornadas de liga, el motor se queda sin muestra ($n < 4$) y descarta casi todos los patrones, pese a tener 20 partidos domésticos recientes del mismo equipo y entrenador.

- **Decisión arquitectónica afectada**: Reglas de agregación en `ObservationStore` (Capa B), `QuantitativePatternEngine` (Capa C2) y `Modeling` (Capa D).
- **Por qué existen varias alternativas**:
  - *Alternativa 4A (Pooling Jerárquico con Contracción por Estrato Competitivo y Decaimiento Temporal)*: Evaluar siempre primero la serie `exact_competition`. Cuando $n_{\text{exact}} < 8$, activar un estimador jerárquico (evolucionando [`prematch_international_expansion.py`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_international_expansion.py)) que combine la muestra del torneo actual con el historial doméstico reciente del mismo régimen de entrenador, ponderado por decaimiento temporal y ajustado por el salto de nivel del rival (SoS inter-competición), etiquetando explícitamente `pooling_mode = "HIERARCHICAL_MULTICOMP"`.
  - *Alternativa 4B (Aislamiento estricto por `league_id` + `season`)*: Mantener la regla actual y declarar `INSUFFICIENT_SAMPLE` siempre que $n_{\text{exact}} < 6$.
  - *Alternativa 4C (Mezcla plana sin ponderación)*: Mezclar todas las competiciones con el mismo peso (como hace hoy `motor_probabilistico.py` en `P-1..P-6`).
- **Mi recomendación como arquitecto**: **Alternativa 4A**. Evita tanto la ceguera en torneos internacionales/copas (4B) como el error de mezclar sin ajuste un partido contra un 3ª RFEF en Copa con uno de Liga (4C).
- **Consecuencias de cada respuesta**:
  - Si eliges **4A**: Podrás analizar Champions/Europa League/Copas y jornadas 1–6 con soporte cuantitativo riguroso, distinguiendo siempre en el contrato qué parte viene del torneo exacto y qué parte del prior doméstico ajustado por SoS.
  - Si eliges **4B**: El motor será prácticamente inoperante en competiciones UEFA, Copas y durante el primer bimestre de cada temporada.
  - Si eliges **4C**: Se introducirá sesgo severo cuando un equipo juegue rondas previas coperas ante rivales muy inferiores.

---

## PREGUNTA 5: Presupuesto de búsqueda y validación en Minería de Patrones (`max_tests=64` y Split 70/30)

En [`prematch_advanced_discovery_v2.py`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_advanced_discovery_v2.py#L238-L407), el split temporal 70% train / 30% holdout sobre $N=8\text{–}10$ partidos detallados deja solo **2 o 3 partidos en el conjunto de test**, haciendo matemáticamente imposible cumplir `min_group >= 3` en el holdout. Además, `max_tests=64` gasta la mayoría de los tests en variables intra-partido (`POSTMATCH_ONLY`).

- **Decisión arquitectónica afectada**: Diseño estadístico de `InteractionMiner` y `PatternValidator` en la Capa C2.
- **Por qué existen varias alternativas**:
  - *Alternativa 5A (Separar `PREMATCH_TRIGGER` vs `INTRA_MATCH_MECHANISM` + Validación Walk-Forward / LOO adaptativa según $N$)*:
    1. Ampliar la ventana de detalles enriquecidos de los candidatos a $N=15\text{–}20$ partidos (aprovechando caché incremental).
    2. Separar el presupuesto de tests: un presupuesto dedicado a condicionales 100% observables antes del partido (`PREMATCH_TRIGGER`, elegibles para modelos) y otro separado para mecanismos internos (`INTRA_MATCH_MECHANISM`, elegibles solo como soporte causal táctico).
    3. Cuando $N < 16$, validar mediante **Leave-One-Out Cross-Validation (LOOCV) + test exacto de permutación/Fisher + BY FDR** sobre la serie temporal; cuando $N \ge 16$, exigir además **Walk-Forward Out-of-Sample** (como en `prematch_predictive_engine.py`).
  - *Alternativa 5B (Mantener el Split fijo 70/30 con `min_group >= 3` y `max_tests=64` global)*.
- **Mi recomendación como arquitecto**: **Alternativa 5A**. Resuelve la imposibilidad matemática actual de validar patrones cuando $N \le 12$ sin relajar el control de falsos positivos (BY FDR).
- **Consecuencias de cada respuesta**:
  - Si eliges **5A**: El motor podrá descubrir y validar interacciones prepartido reales (descanso, estilo rival, localía, árbitro, bajas) con control estricto de FDR.
  - Si eliges **5B**: `TEMPORALLY_VALIDATED_ASSOCIATION` seguirá dando **cero resultados** en casi todos los equipos con $N < 20$ partidos detallados.

---

## PREGUNTA 6: Tratamiento de cambios de Entrenador y Rupturas de Régimen (Alineación / Bajas)

Hoy, si un equipo cambió de entrenador hace 3 semanas o presenta 5 bajas titulares, los motores calculan promedios sobre los últimos 20 partidos como si el equipo fuera el mismo.

- **Decisión arquitectónica afectada**: Lógica de recencia y partición de régimen en `ObservationStore` (Capa B) y `ValidatedPattern` (Capa C2).
- **Por qué existen varias alternativas**:
  - *Alternativa 6A (Partición explícita por régimen de `coach_id` + degradación automática por `squad_stability`)*:
    - Cuando `coach_id` actual difiere del histórico en los últimos 20 partidos, el `ObservationStore` particiona automáticamente la serie en `current_coach_regime` ($n_{\text{coach}}$) y `prior_coach_regime`. Si $n_{\text{coach}} < 5$, cualquier patrón táctico de equipo heredado del técnico anterior se marca como `REGIME_SHIFT_WARNING` (no puede ser `ValidatedPattern` sin corroboración en los partidos del nuevo técnico) y el prior se contrae hacia la media de la liga.
    - Si la estabilidad del XI esperado (`squad_stability`) o la cuota de minutos de titulares ausentes por lesión (`injuries`) supera un umbral (p. ej. falta el máximo rematador o $>30\%$ de minutos de ataque), se activa una bandera `CRITICAL_LINEUP_DELTA` sobre las familias afectadas.
  - *Alternativa 6B (Solo informar el cambio de entrenador como texto para que Codex lo interprete manualmente)*.
- **Mi recomendación como arquitecto**: **Alternativa 6A**. Es la única forma de evitar que los modelos cuantitativos proyecten el estilo de un entrenador destituido antes de que intervenga el LLM.
- **Consecuencias de cada respuesta**:
  - Si eliges **6A**: Los rankings diarios y los modelos probabilísticos dejarán de producir falsos positivos cuando un club acaba de cambiar de cuerpo técnico o rota masivamente.
  - Si eliges **6B**: El escáner diario seguirá recomendando partidos basados en tendencias de entrenadores que ya no están en el banquillo.

---

## PREGUNTA 7: Modelización Probabilística Conjunta del Partido vs. Conteos Individuales por Equipo

En [`prematch_predictive_engine.py`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_predictive_engine.py), los modelos Empirical Bayes + Poisson/Binomial Negativa solo estiman conteos individuales (`own.goals`, `own.corners`), por lo que [`prematch_market_mapping_v1.py`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_market_mapping_v1.py) solo puede mapear mercados "Totales por Equipo", dejando huérfanos los mercados principales de Bwin (Total de Goles del partido, Total de Córners del partido, Ambos Marcan, 1X2, Hándicaps), los cuales caen en el Poisson sin contracción de [`prematch_opportunity_scanner.py`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_opportunity_scanner.py) o en el `hits/6` de [`motor_probabilistico.py`](file:///Users/antonio/Projects/bet-tactical-engine/motor_probabilistico.py).

- **Decisión arquitectónica afectada**: Alcance de la Capa D (`bet_engine.modeling`) y Capa E (`bet_engine.markets`).
- **Por qué existen varias alternativas**:
  - *Alternativa 7A (Unificar en Capa D modelos de Equipo y Totales Conjuntos del Partido)*: Extender el motor predictivo validado por walk-forward para que estime tanto distribuciones por equipo ($\lambda_{\text{home}}, \lambda_{\text{away}}$ ajustadas por concesión rival, SoS y localía con contracción Empirical Bayes) como la **distribución conjunta del partido** (convolución Poisson/NegBin bivariante para Totales FT/H1, BTTS, 1X2 y Hándicaps), jubilando el cálculo ad-hoc de `prematch_opportunity_scanner.py`.
  - *Alternativa 7B (Mantener `prematch_predictive_engine.py` solo para mercados individuales de equipo y usar frecuencias empíricas simples para mercados totales)*.
- **Mi recomendación como arquitecto**: **Alternativa 7A**. Unifica las 5 fórmulas incompatibles de $\lambda$ que hoy coexisten en el repositorio en un único motor probabilístico auditado con walk-forward.
- **Consecuencias de cada respuesta**:
  - Si eliges **7A**: Todos los mercados líquidos de Bwin (totales de partido, por equipo, por mitad, BTTS) se evaluarán bajo el mismo estándar matemático de calibración e intervalos de incertidumbre.
  - Si eliges **7B**: Los mercados principales de goles y córners totales seguirán evaluándose con fórmulas heurísticas distintas a las de los mercados por equipo.

---

## PREGUNTA 8: Integración de Árbitro, Descanso Multicompetición y Calendario Posterior ($T+1$)

Hoy [`RefereeHistoryCollector`](file:///Users/antonio/Projects/bet-tactical-engine/generate_context.py#L2221) descarga hasta 15 partidos del árbitro pero ningún motor lo lee; `rest_hours` en [`prematch_daily.py:459`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_daily.py#L459) ignora partidos entre semana de Champions/Copa; y ningún módulo consulta el próximo partido ($T+1$) para medir riesgo de rotación por *lookahead*.

- **Decisión arquitectónica afectada**: Alcance de `CompetitiveContextBuilder` (Capa B) y plugins de tarjetas/fatiga en `QuantitativePatternEngine` (Capa C2).
- **Por qué existen varias alternativas**:
  - *Alternativa 8A (Integración completa en Capa B y C2)*:
    1. Calcular `rest_hours_all_comps` y `minutes_load_last_14d` cruzando **todas las competiciones** disputadas por el club/jugadores antes de `cutoff_utc`.
    2. Añadir consulta ligera del siguiente fixture programado del equipo ($T+1$ dentro de los próximos 7 días, que ya viene en el catálogo de fixtures o requiere 1 llamada cacheada) para etiquetar `lookahead_rotation_risk`.
    3. Incorporar el perfil estructurado del árbitro (`cards_avg`, `fouls_per_card`, `over_cards_pct`) al `ObservationStore` como condición obligatoria al evaluar mercados de tarjetas/faltas.
  - *Alternativa 8B (Usar estas variables solo como contexto descriptivo en el texto para Codex, sin integrarlas en las señales cuantitativas de C2)*.
- **Mi recomendación como arquitecto**: **Alternativa 8A**. En mercados de tarjetas (sin árbitro no hay línea base real) y en jornadas post/pre-Champions, estas variables cambian por completo la expectativa del partido.
- **Consecuencias de cada respuesta**:
  - Si eliges **8A**: El motor cuantitativo detectará automáticamente oportunidades por fatiga/rotación y cruzará agresividad de los equipos con la severidad real del colegiado designado.
  - Si eliges **8B**: Seguiremos desaprovechando llamadas de API que `generate_context.py` ya está pagando hoy.

---

## PREGUNTA 9: Modo de Ejecución de Codex como Investigador Táctico (Interactivo / Subproceso CLI vs. Handoff por Archivo)

En la nueva arquitectura, el submódulo `C3 (TacticalInvestigator)` entrega a Codex un dossier estructurado y recibe hipótesis con tests `python_test` para que Python (`C2`) las verifique sobre las 20 observaciones antes de emitir el análisis final.

- **Decisión arquitectónica afectada**: Implementación del adaptador `TacticalInvestigator` (Capa C3) y experiencia de uso en `prematch_single.py` / `prematch_daily.py`.
- **Por qué existen varias alternativas**:
  - *Alternativa 9A (Soporte Dual: Handoff por Archivo + Subproceso CLI Automatizable)*: Soportar ambos modos mediante configuración/flag:
    1. **Modo Asistido/Archivo (por defecto)**: Genera `tactical_dossier_<event>.json/.md`, permite que Codex (en tu sesión agéntica o vía CLI) escriba `codex_hypotheses_<event>.json`, ejecuta el verificador Python (`C2`) sobre esas hipótesis y genera el paquete final verificado para `analisis_<event>.md`.
    2. **Modo Subproceso CLI (`--auto-codex` / `--llm-cmd`)**: El orquestador invoca automáticamente al binario CLI configurado en un bucle cerrado (Paso 1: proponer hipótesis `python_test` $\to$ Paso 2: Python las evalúa $\to$ Paso 3: Codex redacta `analisis_<event>.md` con los resultados verificados).
  - *Alternativa 9B (Únicamente flujo manual de un solo paso al final, sin bucle de verificación de hipótesis en Python)*.
- **Mi recomendación como arquitecto**: **Alternativa 9A**. Te da control total en sesiones interactivas como esta y, al mismo tiempo, permite que cuando Codex proponga una hipótesis táctica nueva durante el análisis de un evento, Python la verifique instantáneamente contra los 20 partidos en caché antes de redactar la decisión final.
- **Consecuencias de cada respuesta**:
  - Si eliges **9A**: Codex deja de ser un mero lector pasivo al final del pipeline y se convierte en un verdadero **investigador táctico respaldado por falsación empírica en Python**.
  - Si eliges **9B**: Si a Codex se le ocurre una hipótesis de interacción nueva al leer el partido, no podrá comprobar cuántas veces se cumplió en los partidos `P-7..P-20`.

---

## PREGUNTA 10: Gobernanza para Promover un Patrón a Variable de Modelo Probabilístico (`AcceptedModelFeature`)

Dejaste muy claro en tus principios: *"No debe confundir una hipótesis táctica con un ajuste probabilístico demostrado"* y *"No permitas que Codex invente probabilidades ni modifique automáticamente modelos productivos"*.

- **Decisión arquitectónica afectada**: Frontera entre la Capa C (`ValidatedPattern`) y la Capa D (`AcceptedModelFeature`).
- **Por qué existen varias alternativas**:
  - *Alternativa 10A (Congelación por Registro Auditado — "Frozen Feature Registry")*:
    - Durante la corrida diaria o de un partido, los patrones descubiertos por Python (`C2`) o por Codex (`C3`) pueden alcanzar como máximo el estado `ValidatedPattern` (sirven para priorizar candidatos, explicar mecanismos, alertar riesgos o respaldar/descartar cualitativamente un pick).
    - **Ningún patrón recién descubierto modifica en caliente la probabilidad `p_model` de la Capa D**.
    - Para que un `ValidatedPattern` pase a ser `AcceptedModelFeature` (ajustando matemáticamente $\lambda$), debe superar un **benchmark offline de backtesting walk-forward multi-partido** (`tools/promote_feature_benchmark.py`) demostrando mejora de Brier/Log-Loss y registrarse en un catálogo versionado (`config/accepted_model_features.json`).
  - *Alternativa 10B (Ajuste dinámico en caliente de `p_model` cuando un patrón del partido supere $q \le 0.05$)*.
- **Mi recomendación como arquitecto**: **Alternativa 10A**. Es la única garantía matemática contra el sobreajuste (*overfitting*) en muestras de un solo equipo.
- **Consecuencias de cada respuesta**:
  - Si eliges **10A**: Tus probabilidades `p_model` serán 100% reproducibles, calibradas y libres de contaminación por correlaciones locales; los patrones tácticos del partido actuarán como **filtro de convicción, selección y control de riesgo** junto al modelo probabilístico.
  - Si eliges **10B**: Un patrón local con $n=8$ podría inflar artificialmente `p_model` y fabricar falso EV positivo.

---

## PREGUNTA 11: Optimización del Presupuesto de API-Football y Desactivación de Endpoints Sin Consumidor

La auditoría detectó que [`generate_context.py`](file:///Users/antonio/Projects/bet-tactical-engine/generate_context.py) pide en cada ejecución `predictions` (`GET_PREDICTIONS=True`) y `api_odds` (`GET_API_ODDS=True`), y que [`prematch_phase1.py`](file:///Users/antonio/Projects/bet-tactical-engine/prematch_phase1.py) (en modo no-`core_only`) pide `transfers`, `sidelined`, `topscorers`, `topassists`, `topyellowcards` y `topredcards`, ninguno de los cuales se usa en los modelos ni en las reglas de Bwin.

- **Decisión arquitectónica afectada**: Configuración por defecto de `UnifiedApiFootballClient` (Capa A) y asignación del presupuesto de llamadas por partido.
- **Por qué existen varias alternativas**:
  - *Alternativa 11A (Desactivar por defecto `api_odds`, `predictions`, `transfers` y `top*` redundantes, y reasignar ese presupuesto a ampliar los partidos con estadísticas detalladas de $8 \to 14\text{–}16$)*: Dado que `fixtures/players` de los partidos históricos ya nos da goles, asistencias, tiros, pases, faltas y tarjetas por jugador y por partido (con corte temporal exacto, a diferencia de `topscorers` que acumula toda la temporada sin fecha de corte!), ahorramos entre 6 y 10 llamadas por partido y las invertimos en tener más historia detallada (`fixtures/statistics` + `players` + `events`).
  - *Alternativa 11B (Mantener todos los endpoints actuales activos por defecto)*.
- **Mi recomendación como arquitecto**: **Alternativa 11A**. Además de ahorrar cuota de API y acelerar la ejecución, elimina un riesgo de fuga temporal (`topscorers` / `teams/statistics` de API-Football devuelven acumulados actuales que en auditorías retrospectivas violan `asof_cutoff`).
- **Consecuencias de cada respuesta**:
  - Si eliges **11A**: Con el mismo presupuesto diario de API-Football duplicamos casi la muestra histórica detallada por equipo ($N=14\text{–}16$ en vez de $N=8$), mejorando drásticamente la potencia estadística del núcleo analítico.
  - Si eliges **11B**: Seguiremos gastando llamadas en cuotas/predicciones de API-Football que no se usan y limitando la profundidad histórica detallada a 8 partidos.

---

## PREGUNTA 12: Limpieza y Consolidación de los 4 Wrappers de Partido Único y los 16 Scripts Huérfanos

Actualmente coexisten 4 puntos de entrada para operar sobre eventos seleccionados ([prematch_single.py](file:///Users/antonio/Projects/bet-tactical-engine/prematch_single.py), [prematch_multi.py](file:///Users/antonio/Projects/bet-tactical-engine/prematch_multi.py), [prematch_case.py](file:///Users/antonio/Projects/bet-tactical-engine/prematch_case.py), [prematch_release.py](file:///Users/antonio/Projects/bet-tactical-engine/prematch_release.py)), más 16 scripts diagnósticos/offline en la raíz y 3 archivos `.bak`.

- **Decisión arquitectónica afectada**: Superficie pública de CLI en la raíz del repositorio y estructura de paquetes.
- **Por qué existen varias alternativas**:
  - *Alternativa 12A (Patrón "Strangler Fig" con Fachadas Compatibles)*:
    1. Crear el nuevo paquete modular `bet_engine/` (`contracts`, `acquisition`, `context`, `analytics`, `modeling`, `markets`, `orchestration`).
    2. Mantener en la raíz los comandos principales que usas habitualmente (`prematch_daily.py`, `prematch_single.py`, `prematch_multi.py`, `validate_prematch.py`, `motor_probabilistico.py`, etc.) como **fachadas ligeras 100% retrocompatibles** en argumentos CLI y rutas de salida (`outputs/matches/<EVENT_ID>/...`), delegando internamente en `bet_engine/`.
    3. Mover progresivamente (en la última fase y solo tras pasar todos los tests) los scripts de diagnóstico/auditoría puntual a `tools/legacy/` o `tools/diagnostics/` y eliminar los 3 archivos `.bak`.
  - *Alternativa 12B (Mantener los 73 archivos `.py` en la raíz sin mover ni archivar nada, añadiendo el paquete `bet_engine/` al lado)*.
- **Mi recomendación como arquitecto**: **Alternativa 12A**. Preserva el 100% de compatibilidad con tus comandos actuales y con `AGENTS.md`, mientras limpia la raíz y elimina el código duplicado bajo el capó.
- **Consecuencias de cada respuesta**:
  - Si eliges **12A**: Tus comandos de terminal y flujos de trabajo no se rompen en ningún momento, pero la base de código pasa a ser limpia, testeable por módulo y libre de código muerto.
  - Si eliges **12B**: La raíz seguirá teniendo más de 70 scripts mezclados, dificultando distinguir qué módulos forman parte del motor activo.
