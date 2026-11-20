# Auditoría Individual de Discovery por Fixture (`EVAL3`)

- **Evaluación auditada:** `outputs/discovery/2026-11-16_1500_0300/evaluations/20261116T140316019822Z_39af34fb2084/`
- **Ventana:** `2026-11-16T15:00:00+01:00` → `2026-11-17T03:00:00+01:00`
- **Catálogo Bwin observado (`prospective_trial/bwin/shared/bwin_discovery.json`):** `107` eventos únicos observados across `8` páginas inspeccionadas (`hoy`: 54, `mañana`: 49, `fútbol-4`: 10, `apuestas/competiciones`: 0, `apuestas`: 0, `apuestas/europa-7/uefa-nations-league-0:4`: `EMPTY_OR_BLOCKED` 0, `apuestas/perú-59`: 7, `apuestas/mali-158`: 1).
- **Auditoría de identidad (`prospective_trial/bwin/identity_audit/identity_audit.json`):** `5` `VERIFIED_LINK` (`1528988`, `1528991`, `1545618`, `1549560`, `1649063`), `1` `NAME_MISMATCH` (`1649670`), `11` `SEARCH_INCOMPLETE` (`NOT_FOUND_IN_SCANNED_PAGES` en `bwin_discovery`).
- **Hallazgo central:** De los `11` candidatos clasificados como `NOT_FOUND_IN_SCANNED_PAGES`, **9 estaban literalmente presentes** en `prospective_trial/bwin/shared/catalog_observed.json`, y el `1` candidato bloqueado pese a tener `VERIFIED_LINK` (`1528991` Grecia–Serbia) falló únicamente por un bug operativo de refresco sobre un evento preparado (`outputs/matches/7905477/manifest.json`) al ejecutar `--refresh-existing` sin `--immutable-capture`.

---

## 1. Cobertura API: por qué `historical_matches_available` ≠ `metric_observations_valid`

En `prospective_trial/enriched/preselection.json` (`sample_history_cap = 20`, `sample_details_cap = 8`) y `prospective_trial/tactical/tactical_signal_engine.json`:
1. **`historical_matches_available`** cuenta los partidos FT/AET/PEN previos recuperados en el historial del equipo (hasta `20` en liga peruana/uruguaya; `3–5` recientes disponibles en ventana internacional Nations League).
2. **`historical_matches_detailed`** cuenta cuántos de esos partidos tienen estadísticas de fixture (`statistics`) cacheadas o descargadas bajo el tope `--details 8` (de ahí `9–10` detallados sobre `16–17` disponibles en Liga 1 Perú). Los goles (`goals`) usan el marcador de todos los partidos disponibles (`16–17`), mientras que tiros, córners, faltas, tarjetas y fueras de juego solo existen en los partidos detallados (`8–10`).
3. **`metric_observations_valid` en `coverage_summary`** adopta el **mínimo conservador** entre métricas evaluadas (`xg` suele ser la más restrictiva: p. ej. `8/17` en Perú o `1/5` en Grecia–Serbia, donde varios partidos internacionales previos carecen de `expected_goals` en API-Football; ausencia ≠ cero).

---

## 2. Casos prioritarios 1–6 (auditoría exhaustiva)

### Caso 1 — `1528990` | Germany vs Netherlands (`UEFA Nations League`, ID `5`)
- **Kickoff:** `2026-11-16T20:45:00+01:00`
- **Cobertura API:** `avail=5`, `det=5`, `valid=3` (`xg` 3/5; `goals/corners/shots/on_target/fouls/yellow/offsides` 5/5), `historical_effective_coverage_pct=60.0%`, `ready=70 (REVISION)`. Score estadístico independiente: `69.11` (`5` señales).
- **Discovery Bwin (`bwin_discovery.json`):** `NOT_FOUND_IN_SCANNED_PAGES`, `candidates_found=0`, `discovery_diagnostic=CATALOG_PARTIAL`.
- **Páginas inspeccionadas:** `hoy`, `mañana`, `fútbol-4`, `europa-7/uefa-nations-league-0:4` (`EMPTY_OR_BLOCKED`). No busca país por `league_id=5` (`extend_catalog_for_missing` omite `league_id == 5`).
- **¿Presente en `catalog_observed.json`?** **SÍ.** Evento `7905476`: `'Alemania Países Bajos Hoy / 20:45 CA 1 1.80 X 4.33 2 3.25'` (`https://www.bwin.es/es/sports/eventos/alemania-pa%C3%ADses-bajos-2:7905476`).
- **Causa técnica probada:** `Netherlands` sí traduce a `'paises bajos'` (`away_similarity = 1.0`), pero `'germany'` **no existe** en `NATIONAL_TEAM_TRANSLATIONS` (`bwin_discovery.py:52-73`) ni en `config/bwin_team_aliases.json`, resultando en `home_similarity = 0.0`, `_lexical_shortlist=False`, `cands=0`. `identity_audit.json` arrastra la misma función (`SEARCH_INCOMPLETE`).
- **Estado en Preflight / Scanner / Ledger / CSV:** No llegó a preflight ni scanner; ledger `SIN_CUOTAS`; CSV `RESEARCH_PENDING` (tier C).
- **Fase exacta de bloqueo:** `bwin_discovery` (recuperación léxica inicial).
- **Acción correctiva:** Añadir diccionario completo EN↔ES de selecciones FIFA/UEFA en `NATIONAL_TEAM_TRANSLATIONS` (incluyendo `"germany": ("alemania",)` y búsqueda bidireccional) y registrarlo también en `config/bwin_team_aliases.json`.

### Caso 2 — `1528986` | Kazakhstan vs Slovakia (`UEFA Nations League`, ID `5`)
- **Kickoff:** `2026-11-16T16:00:00+01:00`
- **Cobertura API:** `avail=5`, `det=5`, `valid=4` (`80.0%`), `ready=68 (REVISION)`. Score estadístico: `76.21` (`10` señales, Top-2 del día).
- **Discovery / Audit:** `NOT_FOUND_IN_SCANNED_PAGES`, `candidates_found=0`, `SEARCH_INCOMPLETE` (`NO_TWO_TEAM_CANDIDATE_IN_OBSERVED_CATALOG`).
- **¿Presente en `catalog_observed.json`?** **SÍ.** Evento `7909699`: `'Kazajistán Eslovaquia Comienza en 55 min CA 1 3.90 X 3.60 2 1.85'` (`https://www.bwin.es/es/sports/eventos/kazajist%C3%A1n-eslovaquia-2:7909699`).
- **Causa técnica probada:** `Kazakhstan` cruza contra `'kazajistan'` con `home_similarity = 1.0` por proximidad (`kazaj...` vs `kazakh...`? Nota: en slug `'kazajistan'`, similitud 1.0 en audit por normalización/aproximación), pero `'slovakia'` no tiene `'eslovaquia'` en `NATIONAL_TEAM_TRANSLATIONS` (`away_similarity = 0.0`), impidiendo el shortlist bilateral.
- **Fase exacta de bloqueo:** `bwin_discovery` (traducción faltante `'slovakia' -> 'eslovaquia'`).
- **Acción correctiva:** Incorporar `'slovakia': ('eslovaquia',)` y `'kazakhstan': ('kazajistan', 'kazajstan')` al mapa general EN↔ES.

### Caso 3 — `1545617` | Iceland vs Luxembourg (`UEFA Nations League`, ID `5`)
- **Kickoff:** `2026-11-16T18:00:00+01:00`
- **Cobertura API:** `avail=5`, `det=5`, `valid=4` (`80.0%`), `ready=69 (REVISION)`. Score estadístico: `75.54` (`11` señales, Top-3 del día).
- **Discovery / Audit:** `NOT_FOUND_IN_SCANNED_PAGES`, `candidates_found=0`, `SEARCH_INCOMPLETE`.
- **¿Presente en `catalog_observed.json`?** **SÍ.** Evento `7909705`: `'Islandia Luxemburgo Hoy / 18:00 CA 1 1.21 X 6.50 2 14.50'` (`https://www.bwin.es/es/sports/eventos/islandia-luxemburgo-2:7909705`).
- **Causa técnica probada:** Faltan ambos pares en `NATIONAL_TEAM_TRANSLATIONS`: `'iceland'` (`'islandia'`, `home_similarity=0.0`) y `'luxembourg'` (`'luxemburgo'`, `away_similarity=0.0`).
- **Fase exacta de bloqueo:** `bwin_discovery` (traducción faltante).
- **Acción correctiva:** Incorporar pares EN↔ES de todas las selecciones (`iceland`, `luxembourg`, etc.).

### Caso 4 — `1549558` | Cusco vs ADT (`Primera División Perú`, ID `281`)
- **Kickoff:** `2026-11-16T21:15:00+01:00`
- **Cobertura API:** `avail=16` (`17` Cusco / `16` ADT), `det=9`, `valid=8` (`xg` 8/16; `goals` 17/16; métricas evento 9/10), `eff=47.06%`, `ready=88 (DATOS_APROVECHABLES)`. Score estadístico: `77.88` (`7` señales, **#1 del ranking estadístico**).
- **Discovery / Audit:** `NOT_FOUND_IN_SCANNED_PAGES`, `candidates_found=0`, `SEARCH_INCOMPLETE`. Páginas inspeccionadas incluyen `perú-59` (`OK`, 7 eventos).
- **¿Presente en `catalog_observed.json`?** **SÍ.** Evento `7912772`: `'Cusco FC Tarma Hoy / 21:15 CA'` (`https://www.bwin.es/es/sports/eventos/cusco-fc-ad-tarma-2:7912772`).
- **Causa técnica probada:** `Cusco` empareja (`home_similarity = 1.0`), pero el visitante en API es la sigla `'ADT'` (`Asociación Deportiva Tarma`), mientras que Bwin expone `'Tarma'` en el texto visible y `'ad tarma'` en el slug. Sin sigla institucional en `config/bwin_team_aliases.json`, `away_similarity = 0.0`.
- **Fase exacta de bloqueo:** `bwin_discovery` (sigla/nombre corto `'ADT'` ↔ `'AD Tarma'` / `'Tarma'`).
- **Acción correctiva:** Registrar `adt -> ("ad tarma", "tarma", "asociacion deportiva tarma")` en `config/bwin_team_aliases.json` con trazabilidad al evento observado `7912772` (`CANDIDATE_RETRIEVAL_ONLY_NEVER_IDENTITY_CONFIRMATION`).

### Caso 5 — `1528987` | Faroe Islands vs Moldova (`UEFA Nations League`, ID `5`)
- **Kickoff:** `2026-11-16T16:00:00+01:00`
- **Cobertura API:** `avail=5`, `det=5`, `valid=4` (`80.0%`), `ready=67 (REVISION)`. Score estadístico: `72.91` (`11` señales).
- **Discovery / Audit:** `NOT_FOUND_IN_SCANNED_PAGES`, `candidates_found=0`, `SEARCH_INCOMPLETE`.
- **¿Presente en `catalog_observed.json`?** **SÍ.** Evento `7909698`: `'Islas Feroe Moldavia Comienza en 55 min CA 1 1.85 X 3.40 2 4.33'` (`https://www.bwin.es/es/sports/eventos/islas-feroe-moldavia-2:7909698`).
- **Causa técnica probada:** `Moldova` (`'moldavia'`) puntúa `1.0`, pero `Faroe Islands` (`'islas feroe'`) no está en `NATIONAL_TEAM_TRANSLATIONS` y solo alcanza `home_similarity = 0.4286` (por coincidencia única de `islands`/`islas`), cayendo muy por debajo del umbral `0.82` / shortlist.
- **Fase exacta de bloqueo:** `bwin_discovery` (traducción faltante `'faroe islands' -> 'islas feroe'`).
- **Acción correctiva:** Añadir `'faroe islands': ('islas feroe',)` y `'moldova': ('moldavia',)` en `NATIONAL_TEAM_TRANSLATIONS` y `config/bwin_team_aliases.json`.

### Caso 6 — `1528991` | Greece vs Serbia (`UEFA Nations League`, ID `5`)
- **Kickoff:** `2026-11-16T20:45:00+01:00`
- **Cobertura API:** `avail=5`, `det=5`, `valid=1` (`xg` 1/5 en rival; `goals` y eventos 5/5), `eff=20.0%`, `ready=66 (REVISION)`. Score estadístico: `71.55` (`4` señales).
- **Discovery / Audit:** **`IDENTITY_VERIFIED_AUTOMATICALLY`** (`bwin=7905477`, `VERIFIED_LINK`, `0.0 min` diff, `'Grecia - Serbia UEFA Nations League Fútbol | bwin'`).
- **Preflight (`daily_runs/20261116T150440108353/preflight/1528991_0/market_preflight.json`):** `status=SCRAPER_FAILED_OR_UNVERIFIED`, `scraper.returncode=None`, **`scraper.error="Evento preparado: usar prematch_single.py refresh-odds para actualizar manifest e invalidar derivados"`**, sin clave `snapshot`.
- **Estado en Scanner / Ledger / CSV:** El fallo de `verified_preflight` llamó a `validate_snapshot` sobre un preflight sin `snapshot`, emitiendo el código engañoso `PREFLIGHT_NOT_VALID,EVENT_ID_UNVERIFIED,SNAPSHOT_PATH_OR_SHA_INVALID:REFERENCE_PATH_MISSING`, por lo que `prematch_audit` clasificó el partido como `IDENTIDAD_INCORRECTA` / `BLOQUEADO_IDENTIDAD` aunque la identidad estaba 100% verificada.
- **Causa técnica probada:** `outputs/matches/7905477/manifest.json` ya existe como caso preparado previo (`FINALIZADO` / captura anterior `>120m`). `prematch_runtime.scan()` pasó `--refresh-existing` a `bwin_market_preflight.py` **sin** `--immutable-capture`, provocando que `bwin_market_preflight.py:450` abortara por protección de `manifest.json`. Además, `prematch_runtime.verified_preflight:194-196` exige que `snapshot_path` coincida exactamente con el canónico `outputs/matches/<eid>/inputs/cuotas_bwin_<eid>.txt`, bloqueando también cualquier captura inmutable en `captures/<eid>/<stamp>/`.
- **Fase exacta de bloqueo:** `capture / preflight` (no es bloqueo de identidad).
- **Acción correctiva:**
  1. Cuando exista `outputs/matches/<eid>/manifest.json` y se solicite `--refresh-existing-odds`, usar `--immutable-capture` para guardar la nueva captura fresca bajo `preflight/.../captures/<eid>/<stamp>/` sin sobrescribir el `manifest.json` canónico ni su TXT protegido.
  2. Permitir en `verified_preflight` dicha ruta inmutable verificada solo cuando el evento tiene `manifest.json` protegido y coincide el `sha256` declarado.
  3. Propagar errores de `scraper` (p. ej. `SCRAPER_REFRESH_BLOCKED:...`) para no etiquetarlos falsamente como `IDENTIDAD_INCORRECTA`.

---

## 3. Candidatos restantes 7–17 (`EVAL3`)

| # | Fixture ID | Partido | Competición (`league_id`) | Kickoff Madrid | API (`avail/det/val`, `eff%`, `stat_score`) | Discovery / Audit | En `catalog_observed.json`? | Fase bloqueo / estado final | Causa exacta y acción |
|---:|---:|---|---|---|---|---|---|---|---|
| 7 | `1549556` | UTC Cajamarca vs Club Deportivo Los Chankas | Primera División (`281`) | `19:00` | `17/9/6` (`35.29%`), `74.5` | `NOT_FOUND` (`cands=0`) / `SEARCH_INCOMPLETE` | **SÍ** (`7912771`: `UTC de Cajamarca CD Los Chankas CYC Hoy / 19:00 CA`) | `bwin_discovery` -> `RESEARCH_PENDING` | `'club'` se elimina como stop-token y quedan `3` tokens `['deportivo','los','chankas']`; Bwin abrevia `'deportivo'` a `'cd'`, dejando `away_similarity=0.4821`. Plegar `'club deportivo X' -> 'cd X'` + alias `7912771`. |
| 8 | `1528988` | Estonia vs Bulgaria | UEFA Nations League (`5`) | `18:00` | `5/5/4` (`80.0%`), `73.31` | `IDENTITY_VERIFIED` (`7909704`) / `VERIFIED_LINK` | **SÍ** (`7909704`) | **Ninguno — `SELECCIONADO`** | `564` cuotas reutilizadas vía sibling preflight, `scanner=VERIFIED`, `tier=A`, `MANUAL_RESEARCH_ONLY`. |
| 9 | `1545662` | FYR Macedonia vs Slovenia | UEFA Nations League (`5`) | `20:45` | `5/5/4` (`80.0%`), `70.95` | `NOT_FOUND` (`cands=0`) / `SEARCH_INCOMPLETE` | **SÍ** (`7909662`: `Macedonia del Norte Eslovenia Hoy / 20:45`) | `bwin_discovery` -> `RESEARCH_PENDING` | `FYR Macedonia` traduce bien, pero falta `'slovenia': ('eslovenia',)` (`away_similarity=0.0`). Corregido en diccionario EN↔ES. |
| 10 | `1545652` | Switzerland vs Scotland | UEFA Nations League (`5`) | `20:45` | `5/5/4` (`80.0%`), `69.5` | `NOT_FOUND` (`cands=0`) / `SEARCH_INCOMPLETE` | **SÍ** (`7909663`: `Suiza Escocia Hoy / 20:45`) | `bwin_discovery` -> `RESEARCH_PENDING` | `Scotland` traduce bien (`1.0`), falta `'switzerland': ('suiza',)` (`home_similarity=0.0`). Corregido en diccionario EN↔ES. |
| 11 | `1549560` | UCV Moquegua vs Atletico Grau | Primera División (`281`) | `21:15` | `17/8/4` (`23.53%`), `67.71` | `IDENTITY_VERIFIED` (`7912773`) / `VERIFIED_LINK` | **SÍ** (`7912773`) | **Ninguno — `ANALIZADO_PARA_INVESTIGACION`** | `148` cuotas, `scanner=VERIFIED`, `tier=B`. |
| 12 | `1528989` | Lithuania vs Liechtenstein | UEFA Nations League (`5`) | `18:00` | `3/3/3` (`100.0%`), `0` | `NOT_FOUND` (`cands=0`) / `SEARCH_INCOMPLETE` | **SÍ** (`7909690`: `Lituania Liechtenstein Hoy / 18:00 CA`) | `bwin_discovery` -> `SIN_CUOTAS` | `Liechtenstein` coincide (`1.0`), falta `'lithuania': ('lituania',)` (`home_similarity=0.0`). Corregido en diccionario EN↔ES. |
| 13 | `1545618` | Gibraltar vs Malta | UEFA Nations League (`5`) | `20:45` | `3/3/3` (`100.0%`), `0` | `EVENT_FOUND_NEEDS_TIME_CONFIRMATION` (`7909684`) / `VERIFIED_LINK` | **SÍ** (`7909684`) | **Ninguno — `ANALIZADO_PARA_INVESTIGACION`** | Hora reconciliada vía catálogo (`Hoy / 20:45`), `550` cuotas, `scanner=VERIFIED`, `tier=C`. |
| 14 | `1647127` | Nacional vs La Fama | Division di Honor Aruba (`421`) | `01:30 (+1)` | `3/0/0` (`0.0%`), `0` | `NOT_FOUND` (`cands=0`, `NAVIGATION_UNRESOLVED`) / `SEARCH_INCOMPLETE` | **NO** | `catalog_navigation` -> `SIN_CUOTAS` | Bwin no expone enlace para `'aruba'` en menú de competiciones ni aparece en `/hoy` / `/mañana`. Ausencia de ruta navegable, no error de alias. |
| 15 | `1649063` | Wanderers vs Juventud | Primera División Uruguay (`268`) | `00:00 (+1)` | `14/8/0` (`0.0%`), `0` | `IDENTITY_VERIFIED` (`7912816`) / `VERIFIED_LINK` | **SÍ** (`7912816`) | **Ninguno — `ANALIZADO_PARA_INVESTIGACION`** | `258` cuotas, `scanner=VERIFIED`, `tier=B`. |
| 16 | `1649669` | Diarra vs ASKO | Première Division Mali (`598`) | `17:00` | `2/0/0` (`0.0%`), `0` | `NOT_FOUND` (`cands=0`, `CATALOG_PARTIAL`) / `SEARCH_INCOMPLETE` | **NO** | `bwin_catalog` -> `SIN_CUOTAS` | `mali-158` cargó `OK`, pero solo mostró `1` evento (`7912836`); Bwin no listaba este partido en el momento de captura (`0` detalles API igualmente). |
| 17 | `1649670` | Bakaridjan vs Derby Academie | Première Division Mali (`598`) | `19:30` | `2/0/0` (`0.0%`), `0` | `EVENT_REQUIRES_REVIEW` (`7912836`) / `NAME_MISMATCH` | **SÍ** (`7912836`: `AS Bakaridjan - Derby Academie Ligue 1`) | **Legítimamente bloqueado (`BLOQUEADO_IDENTIDAD`)** | Conflicto real doble: Bwin muestra `Ligue 1` vs `Première Division` (`competition_similarity=0.3214`) y `kickoff_difference_minutes=30.0` (`19:00` vs `19:30`). **No relajar.** |

---

## 4. Limitaciones adicionales detectadas en el descubrimiento de catálogo

1. **`europa-7/uefa-nations-league-0:4` devolvió `EMPTY_OR_BLOCKED (0 eventos)`:** En `EVAL3`, todos los partidos UNL aparecieron igualmente en `/hoy` (`54` eventos), pero si algún encuentro futuro solo estuviese en la página de competición y esta tardase más en hidratar enlaces DOM, conviene dejar registrada la traza sin marcarlo como ausente (`CATALOG_PARTIAL`).
2. **Enmascaramiento en `candidate_ledger.json` para eventos preparados (`1528991`):** Cuando `bwin_market_preflight --run-scraper --refresh-existing` es rechazado por protección de `manifest.json`, la falta de clave `snapshot` desencadenaba `SNAPSHOT_PATH_OR_SHA_INVALID:REFERENCE_PATH_MISSING` y mapeaba erróneamente a `IDENTIDAD_INCORRECTA` pese a existir `VERIFIED_LINK`.
