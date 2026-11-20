# Diagnóstico de Causa Raíz — Descubrimiento, Cobertura, Ranking y Bwin (Barrido 18/11/2026)

## 1. Problema 1 — Filtrado prematuro de competiciones (`filter_major_leagues.py`)
- **Evidencia observada (`outputs/discovery/2026-11-18_1500_0300/evaluations/20261118T134610142084Z_c515d959b536/league_filter_review.json`):**
  - El barrido del 18/11/2026 se ejecutó con `competition_scope = "domestic-top-tier"` (`evaluation_manifest.json`).
  - `filter_major_leagues.classify()` evaluaba primero `HARD_STOP = re.compile(r'(?i)(?:...|\bcopa\b|\bcup\b|\bchampions\s*league\b)')` y devolvía `('EXCLUDED', 'YOUTH_RESERVES_OR_CUP')` de forma indiferenciada.
  - Por ello, `UEFA Champions League Women` (League ID `525`, ya catalogada en `config/competition_registry.json` como `SENIOR`, `gender='F'`, `NATIONAL_OFFICIAL`, `ELIGIBLE`), `Copa del Rey` (`143`, `SENIOR`, `M`) y `WSL Cup` (`697`) quedaron etiquetadas bajo `YOUTH_RESERVES_OR_CUP` junto con `Copa do Brasil U20` (`617`) y `Argentina Reserve League` (`906`).
  - Al mismo tiempo, `Dinamo Minsk II vs BATE II` (`league_id=117`, Belarus `1. Division`) cayó en `REVIEW` (`NOT_IN_CURATED_TOP_TIER_CATALOG`) porque `domestic-top-tier` no aplicaba `YOUTH_PATTERN` / `RESERVE_TEAM_PATTERN` antes de comprobar `CATALOG_BY_COUNTRY`.
- **Inconsistencia adicional detectada (`bwin_discovery.py`):**
  - `bwin_discovery.read_candidates()` aplicaba `competition_registry.category()` antes que `explicit_competition_category()`, sobrescribiendo `SEASON_COMPETITION_REGISTRY` con `EXPLICIT_COMPETITION_CATEGORY_CATALOG` (causando el fallo heredado `test_leganes_category_propagates_through_real_contracts`).

## 2. Problema 2 — Cobertura estadística discriminatoria (`preselect_matches.py`, `rank_matches.py`)
- `score_readiness()` sumaba `METRIC_WEIGHTS` sobre 7 métricas (`corners`, `shots`, `on_target`, `fouls`, `offsides`, `yellow`, `xg`, 60 puntos) sin evaluar `goals` por familia independiente.
- Como resultado, un partido con 20/20 históricos de goles (`1650229`, Liga Panameña) recibió `data_readiness_status = 'COBERTURA_LIMITADA'` por carecer de estadísticas de córners/tiros/tarjetas/xG, mientras que `rank_matches.rank_report()` exigía `data_readiness_status == 'DATOS_APROVECHABLES'`.
- Además, `apply_coverage_priority()` añadía `advanced_bonus` (hasta +7.0 puntos) por capacidades declaradas de API (`events`, `lineups`, `statistics_players`, `players`), y `prematch_tactical_signal_engine.analyze_fixture()` / `prematch_daily.operational_research()` sumaban ese bono al score de ranking.

## 3. Problema 3 — Ranking sesgado por cobertura bruta y ruido muestral (`prematch_tactical_signal_engine.py`)
- En el `tactical_signal_engine.json` real del 18/11/2026:
  - `build_signal()` asignaba 20% a `coverage` (`historical_effective_coverage_pct`) y 20% a `sample` (`min(n/10, 1)`), sin contracción empírica por incertidumbre muestral. Así, `Cotonsport vs Stade Renard` (`1645880`, Camerún) con solo `n=4` partidos históricos obtuvo `effective_coverage = 100.0%` (`62.90` puntos), mientras que `Coritiba vs Santos` (`1492451`, Brasil Serie A) con `15` históricos y `detail_limit=8` quedó penalizado con `42.11%` de cobertura efectiva (`< 50%` exigido en `usable`).
  - En `selection_rows()`, `1650229` (Panamá, solo 2 señales en `goals`, 0 contradicciones por ausencia de otras métricas) obtuvo `research_priority = 83.5` (Puesto #1, `SELECCIONADO`), superando a `1492451` (`Coritiba vs Santos`, `tactical_research_score = 79.4`, `research_priority = 81.64`) y `1492454` (`Gremio vs Bahia`, `78.44` / `81.06`), a los que se restó `-6.0` por contradicciones en familias secundarias no presentes en Panamá.

## 4. Problemas 4 y 5 — Bwin y presupuestos (`prematch_runtime.py`, `prematch_daily_csv.py`)
- `1628569` (Nigeria, `NOT_FOUND_IN_SCANNED_PAGES`, `signal_count=0`) terminó en `partidos_diarios.csv` con `estado_final = SIN_CUOTAS` en lugar de reflejar búsqueda incompleta/no encontrada sin descartar oferta.
- `1492452`, `1492453` y `1492456` (Brasil Serie A) quedaron diferidos por `max_scrapes=5` (`DIFERIDO_POR_PRESUPUESTO`), pero `prematch_daily_csv.export_daily()` sobrescribía `NO_EVALUADO_POR_LIMITE` con `RESEARCH_PENDING` cuando había `signal_count > 0`, perdiendo la visibilidad explícita de `PENDING_BUDGET`, y `selection_rows()` los enviaba a `selection_tier = C` con `hard_block_reasons = ['NO_USABLE_VERIFIED_MARKET', 'SCANNER_OR_BWIN_NOT_VERIFIED']`.
- No existía CLI directa para asociar manualmente una o varias URLs de eventos Bwin (`<fixture_id>=<url>`) con revalidación completa de identidad y trazabilidad separada de URL proporcionada, identidad verificada y captura de cuotas.
