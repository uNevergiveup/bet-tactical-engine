# Reporte Cualitativo PREMATCH — Chelsea W vs Paris Saint Germain W

- **Partido:** Chelsea W vs Paris Saint Germain W
- **Competición:** UEFA Champions League Women — Fase de Liga, Jornada 5 (`league_id=525`, `season=2026`, `League Stage - 5`)
- **fixture_id:** 1638301
- **event_id_bwin:** 7913441
- **URL Bwin verificada:** https://www.bwin.es/es/sports/eventos/chelsea-f-psg-f-2:7913441
- **kickoff_utc:** 2026-11-18T20:00:00+00:00
- **kickoff_madrid:** 2026-11-18T21:00:00+01:00
- **analysis_cutoff_utc:** 2026-11-18T19:34:24.616116+00:00
- **report_generated_utc:** 2026-11-18T19:40:00+00:00
- **report_completed_madrid:** 2026-11-18T20:40:00+01:00
- **xi_local_estado:** PUBLICADO_PREPARTIDO (`API_FOOTBALL / STRUCTURED_PROVIDER`, conciliado con `chelseafc.com`; `primary_official_verified=false`)
- **xi_visitante_estado:** PUBLICADO_PREPARTIDO (`API_FOOTBALL / STRUCTURED_PROVIDER`; `primary_official_verified=false`)
- **xi_local_fuente:** `API_FOOTBALL / STRUCTURED_PROVIDER` (`context_updates/4bde42efcc81581fded11234/acquisition_record.json`) + `chelseafc.com` (https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEu0l0qB8egK4o-_5tR-dKOtgCxH39XHkpjBEnevQu0k2111eb6Cm8NmABwujp_chBPKeDQmh_Myq173437UKVDQQ9pO6kpdTynC4b7f24s6cvPMmwQqQvSTszBe1HoSExHW6mBp8lPT3CxTPsy6cYzn_jlgA--YhP2d8_dVQAcmJpStfj7ww==)
- **xi_visitante_fuente:** `API_FOOTBALL / STRUCTURED_PROVIDER` (`context_updates/4bde42efcc81581fded11234/acquisition_record.json`, `lineup_audit_7913441.json`) + convocatoria previa `coeursdefoot.fr` (https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQGVQBbqITjDyzz6CP-SgJFH88B4bv0WH_o3hOknrvd_PGLWaNpHU3c14AmYXS-zb8v_6i0WtyBAjUpZXEQQCiRBYID2s0O8y1W-V8xvr6CHhG5reL7tTBcNSwYZEXVvqJ80XmVlfEXu2eQ-qwpxuZxgEfL-GkGM3hQltkZEBtYq2PTHd_pvPytMYtPceRzi1sKULKzmIe-PLuok)
- **Nota operativa de adquisición:** El informe inicial (`2026-11-18T19:30:00+00:00`) se elaboró con `search_web` (al no estar disponible `chrome_devtools` MCP en el subagente `/browser`) y el contexto local inicial (`19:16:45 UTC`). Se actualiza a las `2026-11-18T19:40:00+00:00` (`20:40:00+01:00`) con la adquisición diferencial de los 22 titulares de API-Football (`2026-11-18T19:34:24.616116+00:00`). **No se generan picks, cuotas objetivo, probabilidades ni estimaciones de EV.**

---

## Registro de Actualización (2026-11-18T19:40:00+00:00 / 20:40:00+01:00)

- **Procedencia:** `API_FOOTBALL / STRUCTURED_PROVIDER` (`official_source_tier: STRUCTURED_PROVIDER`, `primary_official_verified: false` — proveedor estructurado, no equivale por sí solo a certificación federativa oficial de UEFA).
- **Artefactos leídos:**
  - `outputs/matches/7913441/lineups/lineup_audit_7913441.json` (`refreshed_at_utc: 2026-11-18T19:34:24.632180+00:00`, `acquired_at_utc: 2026-11-18T19:34:24.616116+00:00`, `source_sha256: 962d958a59481c3ecfb82a249994d6c4cd5b0efd3545ce4a082c9bffd31f7655`, `status: LINEUPS_API_CONFIRMED`, `COMPLETE_22_STRUCTURED`).
  - `outputs/matches/7913441/context_updates/4bde42efcc81581fded11234/lineup_delta.json` (`status: API_STRUCTURED_NOT_OFFICIAL_CERTIFICATION`, `changed: true`, `formation_changed: true` y `coach_changed: true` en ambos equipos al pasar de ausencia previa de XI a `3-4-3` y `4-4-2`).
  - `outputs/matches/7913441/context_updates/4bde42efcc81581fded11234/acquisition_record.json` (`fetched_at_utc: 2026-11-18T19:34:24.616116+00:00`, `payload_sha256: 96ecc07dba9a7845362dccb7eecc495abb856ed461db394a2effd77c83c87fa3`).
- **Cambios clave respecto a la versión inicial (`19:30:00+00:00`):**
  1. **Chelsea W (`3-4-3`, `S. Bompastor id=14526`):** Confirmación exacta en API de los `11` titulares ya reportados por `chelseafc.com`. La cuadrícula API refleja `3-4-3` (`Björn`–`McCabe`–`Buurman` en línea de tres; `Carpenter` y `Thompson` en carriles; `Walsh`–`Potter` en medio; `Kaptein`–`Beever-Jones`–`Baltimore` arriba) y añade a **`L. Bronze (#22)`** en un banquillo de `10` jugadoras (no figuraba en la nota previa de `9`).
  2. **PSG W (`4-4-2`, `Paulo César id=17233`):** Corrección sustancial sobre el XI probable previo. **Ni `Paulina Dudek (#4)` ni `Olga Carmona (#77)` son titulares** (ambas parten en el banquillo). La zaga titular es **`Belinha (#12)` – `G. Mbock Bathy (#29)` – `T. Samoura (#2)` – `T. Elimbi (#20)`**; la línea de cuatro medios reúne a **`O. Echegini (#6)` – `J. Groenen (#14)` – `E. Østerås (#30)` – `J. Le Guilly (#28)`**; y arriba actúan **`N. Feller (#18)` y `M. Kanjinga (#19)`**. En el banquillo (`12`) consta `O. Szperkowska (#16)` en lugar de `O. Peda` respecto a la lista previa de prensa.
  3. **Integridad histórica:** Conforme a `lineup_delta.json`, las estadísticas históricas congeladas y líneas base predictivas del equipo permanecen sin cambios (`unchanged_models`), afectando únicamente a identidad/exposición de jugadoras e interpretación táctica del partido objetivo.

---

## 1. Integridad, Identidad y Discrepancias de Contexto

- **Identidad del evento [`CONFIRMADO`]:** `match_identity.json` y `match_engine_7913441.json` vinculan inequívocamente el evento Bwin `7913441` con el fixture API `1638301` (`Chelsea W` ID `1853` vs `Paris Saint Germain W` ID `1667`, categoría femenina `"F"`, kickoff `2026-11-18T20:00:00+00:00` / `2026-11-18T21:00:00+01:00`, diferencia `0.0` min con `INSPECTED_EVENT_PREMATCH_HEADER` de Bwin).
- **Estado del fixture y adquisiciones temporales [`CONFIRMADO`]:** El contexto inicial (`V5.7-MICRO`) se generó a las `2026-11-18T18:51:10.203330+00:00` (`status=NS`). La adquisición de las `19:16:45.515488+00:00` (`82e45674...json`) devolvió `results=0`; la adquisición diferencial de las **`2026-11-18T19:34:24.616116+00:00`** (`4bde42ef...`) incorporó los `22` titulares y `22` suplentes (`10` local + `12` visitante) desde `API_FOOTBALL / STRUCTURED_PROVIDER`.
- **Sede del encuentro [`CONTRADICCIÓN DOCUMENTADA / NO VERIFICADO UNÍVOCAMENTE`]:**
  - En API-Football (`match_engine_7913441.json`) figura `Wheatsheaf Park, Staines` (`venue.id=None`).
  - Las fuentes web (`chelseafc.com`, `theguardian.com`, `vavel.com`, `livescore.com`, `coeursdefoot.fr`) sitúan el choque en **The Cherry Red Records Stadium** en Londres, con referencias mezcladas entre **Kingsmeadow** y **Plough Lane / AFC Wimbledon Stadium** (ambos compartieron históricamente dicho nombre comercial). Queda **CONFIRMADO** que no se juega en Stamford Bridge y **NO VERIFICADO** unívocamente entre Kingsmeadow y Plough Lane sin inspección visual directa de la ficha UEFA.
- **Árbitra [`CONFIRMADO`]:** **Maria Sole Ferrieri Caputi** (Italia), coincidente entre `match_engine_7913441.json` y las previas del encuentro.
  - En `referee_history` (`2026-05-22` a `2026-11-18`) solo constan `n=2` partidos UWCL (`SL Benfica W 0-3 Bayern Munich W` el `30/09/2026`, `2` amarillas, `0` rojas, `11` faltas; y fixture `1623466` el `26/08/2026` sin desglose completo de faltas), media `1.5` amarillas y `0.0` rojas/penaltis. Por `n=2`, el perfil disciplinario es **INSUFICIENTE / NO VERIFICADO**.
- **Meteorología [`PROBABLE`]:** En Londres se reporta temperatura en torno a `9 °C` con brisa ligera (`spilxperten.com`). Sin parte oficial del estadio (`NO VERIFICADO`).

---

## 2. Situación Clasificatoria, Descanso y Calendario

- **Clasificación Fase de Liga UWCL tras 4 jornadas (`match_engine_7913441.json`) [`CONFIRMADO`]:**
  - **Chelsea W (`5.º` puesto):** `4 PJ`, `2V - 2E - 0D`, `3 GF - 0 GC`, `8 pts`, racha europea `D-W-D-W`. Único equipo junto a Lyon W (`#1`, `20:0`) sin encajar un solo gol tras 4 jornadas europeas. Pelea por entrar en el **Top-4** (cuartos directos, empatado a `8 pts` con Bayern `#3` y Arsenal `#4`).
  - **Paris Saint Germain W (`10.º` puesto):** `4 PJ`, `1V - 2E - 1D`, `8 GF - 4 GC`, `5 pts`, racha europea `L-D-W-D`. Cayó `1-2` en casa ante Bayern (`10/11/2026`) tras empatar `1-1` en Mánchester frente al City (`29/10/2026`) y golear `5-0` al OH Leuven (`01/10/2026`). En zona de playoff (`5.º–12.º`), con `+1` sobre Køge/Roma/Inter (`4 pts`).
- **Descanso y carga competitiva [`CONFIRMADO`]:**
  - **Chelsea W:** Último partido el `2026-11-14T13:30:00+00:00` (`Chelsea W 1-0 Manchester City W`). **`102.5 horas` (`~4.27 días`)** de descanso; próximo compromiso `2026-11-21T12:45:00+00:00` ante `Liverpool W` (`~64.75 horas` / `2.70 días` después).
  - **PSG W:** Último partido el `2026-11-14T20:00:00+00:00` (`Marseille W 0-2 PSG W`). **`96.0 horas` (`4.00 días`)** de descanso; próximo compromiso `2026-11-21T20:00:00+00:00` ante `Saint-Malo W` (`72.0 horas` / `3.00 días` después).
- **Antecedentes directos (`H2H` en JSON) [`CONFIRMADO`]:** `2` duelos de 2022 (`20/10/2022` `PSG W 0-1 Chelsea W` y `22/12/2022` `Chelsea W 3-0 PSG W`), obsoletos por relevo de plantillas y técnicos.

---

## 3. Entrenadores, Esquemas y Dinámica Táctica Reciente

- **Chelsea W — Sonia Bompastor (`coach_id=14526`, en el cargo desde `2024-07-01`) [`CONFIRMADO`]:**
  - Continuidad estructural plena. En sus `5` partidos previos alternó `3-4-3`/`3-4-2-1` (vs `Häcken W 0-0`, `Everton W 1-1`, `Charlton 6-0`, `Roma W 2-0`) y `4-2-3-1` (vs `Manchester City W 1-0` el `14/11/2026`). Para hoy regresa al **`3-4-3`** declarado en `API_FOOTBALL / STRUCTURED_PROVIDER`.
  - **Métricas recientes [`CONFIRMADO` en API]:** Alto volumen ofensivo y de córners (`78%`, `32` tiros, `13` córners vs `Häcken W`; `65%`, `20` tiros, `13` córners vs `Everton W`; `60%`, `21` tiros vs `Roma W`) con concesión baja (`3`–`6` tiros rivales en 5 de sus últimos 6 partidos; `13` vs Man City). Entre `5` y `12` faltas y `0`–`3` amarillas en los últimos `6` encuentros.
  - **Declaraciones prepartido (`chelseafc.com`):** Destacó que dependen de sí mismas para entrar en el Top-4 ganando sus dos compromisos restantes.
- **Paris Saint Germain W — Paulo César (`coach_id=17233`, inicio `2026-10-01`) [`CONFIRMADO`]:**
  - **Cambio reciente de régimen (`~7 semanas`):** No extrapolar tendencias anteriores al `01/10/2026`. Con él se habían observado `4-2-3-1` (`Marseille W 0-2 PSG W` el `14/11`), `4-1-4-1` (`1-2` vs Bayern, `1-1` en Man City) y `4-3-3` (`4-0` Le Havre, `1-1` Paris FC). Hoy introduce **`4-4-2`** en `API_FOOTBALL / STRUCTURED_PROVIDER`, repitiendo la pareja de centrales `Mbock Bathy`–`Samoura` de Marsella pero sin `Dudek` ni `Olga Carmona` en el once inicial.
  - **Patrones recientes [`CONFIRMADO` en API]:** Fuera ante Man City (`34%` posesión, `9` vs `15` tiros, `1` vs `7` córners, `13` faltas) y contra Bayern (`44%`, `8` vs `10` tiros, `4` vs `6` córners, `18` faltas) cedió iniciativa y aumentó faltas cometidas. Paulo César subrayó en `psg.fr` el peligro de **Naomie Feller** en `1v1` y regate.

---

## 4. Bajas, Convocatorias y Disponibilidad de Jugadoras

- **Nota de cobertura API [`CONFIRMADO`]:** `fixtures/injuries` (`18:51:07 UTC`) devolvió `results=0`; las bajas se documentan mediante prensa oficial y la lista definitiva de `21` (`Chelsea W`) y `23` (`PSG W`) jugadoras en `acquisition_record.json`.

### Chelsea W (`11` titulares + `10` suplentes en `API_FOOTBALL`)
- **Melvine Malard (`#9`, delantera) — `CONFIRMADO` (BAJA):** Lesión de isquiotibiales ante Man City (`14/11/2026`); fuera hasta tras el parón (`chelseafc.com`). Ausente del acta (`11+10`).
- **Lauren James (`#10`, atacante) — `CONFIRMADO` (BAJA):** Isquiotibiales desde `01/11/2026` vs Charlton. Ausente del acta (`11+10`).
- **Naomi Girma (`#4`, defensa) — `CONFIRMADO` (BAJA):** Rehabilitación. Ausente del acta (`11+10`).
- **Manaka Matsukubo (`#29`, centrocampista) — `CONFIRMADO` (NO CONVOCADA):** Aún sin alta competitiva (`chelseafc.com`). Ausente del acta (`11+10`).
- **Kadeisha Buchanan (`#26`), Giulia Dragoni (`#16`) y Lucy Bronze (`#22`) — `CONFIRMADO` (EN BANQUILLO):** Las tres integran los `10` suplentes en `acquisition_record.json` (desmintiendo fuentes secundarias que daban de baja a `Buchanan`, e incorporando a `Bronze` que no aparecía en el resumen inicial de `9` suplentes de `search_web`).

### Paris Saint Germain W (`11` titulares + `12` suplentes en `API_FOOTBALL`)
- **Sakina Karchaoui (`#7`, capitana / lateral-extremo) — `CONFIRMADO` (BAJA):** Esguince en tobillo derecho vs Man City (`29/10/2026`, 6–7 semanas). Ausente de los `23` en Londres.
- **Grace Geyoro — `CONFIRMADO` (NO PERTENECE AL CLUB):** Traspasada previamente a London City Lionesses (`psg.fr`).
- **Olga Carmona (`#77`) y Paulina Dudek (`#4`) — `CONFIRMADO` (SUPLENTES):** Forman parte de los `12` suplentes en `API_FOOTBALL` (`lineup_delta.json`/`acquisition_record.json`), corrigiendo la hipótesis previa que las situaba de inicio.
- **Ajuste de porteras suplentes:** En el banquillo API figura `O. Szperkowska (#16)` junto a `O. Toussaint (#33)` (mientras `O. Peda` no consta entre las `23` de `acquisition_record.json`).
- **Ausentes de las `23` respecto al `squads.away` histórico de API [`NO VERIFICADO` motivo individual]:** `M.-A. Katoto`, `M. Earps`, `É. de Almeida`, `E. Gaetino`, `A. Tounkara`, `K. Albert`, `C. Dunn`, `L. Fazer`, `L. Martens` y `R. Leuchter`.

---

## Alineaciones y Banquillos

- **Estado de conciliación (`19:34:24+00:00`):** Ambos onces (`11` + `11`) y banquillos (`10` + `12`) constan estructurados en `API_FOOTBALL / STRUCTURED_PROVIDER` (`lineup_audit_7913441.json`, `COMPLETE_22_STRUCTURED`). Se mantiene `primary_official_verified=false` (el proveedor estructurado no equivale por sí solo a certificación federativa primaria).

### Chelsea W — PUBLICADO PREPARTIDO (`3-4-3`, `API_FOOTBALL / STRUCTURED_PROVIDER` + `chelseafc.com`)

| N.º | Dorsal | Jugadora | Posición / Cuadrícula API (`3-4-3`) | Estado |
|---:|---:|---|---|---|
| 1 | 24 | H. Hampton | POR (`G`, `1:1`) | Titular confirmado proveedor |
| 2 | 14 | N. Björn | DFC (`D`, `2:3`) | Titular confirmado proveedor |
| 3 | 15 | K. McCabe | DFC / Central izq. (`D`, `2:2`) | Titular confirmado proveedor |
| 4 | 5 | V. Buurman | DFC (`D`, `2:1`) | Titular confirmado proveedor |
| 5 | 2 | E. Carpenter | Carrilera derecha (`M`, `3:4`) | Titular confirmado proveedor |
| 6 | 21 | K. Walsh | Mediocentro (`M`, `3:3`) | Titular confirmado proveedor |
| 7 | 32 | L. Potter | Mediocentro (`M`, `3:2`) | Titular confirmado proveedor |
| 8 | 12 | A. Thompson | Carrilera izquierda (`M`, `3:1`) | Titular confirmado proveedor |
| 9 | 19 | W. Kaptein | Atacante derecha (`F`, `4:3`) | Titular confirmado proveedor |
| 10 | 33 | A. Beever-Jones | Delantera centro (`F`, `4:2`) | Titular confirmado proveedor |
| 11 | 11 | S. Baltimore | Atacante izquierda (`F`, `4:1`) | Titular confirmado proveedor |

- **Suplentes Chelsea W (`10`, `acquisition_record.json`) [`CONFIRMADO` proveedor]:** `L. Peng (#1, G)`, `R. Spencer (#38, G)`, `L. Bronze (#22, D)`, `K. Buchanan (#26, D)`, `E. Cuthbert (#8, M)`, `S. Nüsken (#6, M)`, `G. Dragoni (#16, M)`, `L. Brown (#25, M)`, `N. Las (#17, M)`, `M. Hamano (#23, F)`.

### Paris Saint Germain W — PUBLICADO PREPARTIDO (`4-4-2`, `API_FOOTBALL / STRUCTURED_PROVIDER`)

| N.º | Dorsal | Jugadora | Posición / Cuadrícula API (`4-4-2`) | Estado |
|---:|---:|---|---|---|
| 1 | 1 | K. Kiedrzynek | POR (`G`, `1:1`) | Titular confirmado proveedor |
| 2 | 12 | Belinha (Isabela Chagas) | Lateral derecha (`D`, `2:4`) | Titular confirmado proveedor |
| 3 | 29 | G. Mbock Bathy | Defensa central (`D`, `2:3`) | Titular confirmado proveedor |
| 4 | 2 | T. Samoura | Defensa central (`D`, `2:2`) | Titular confirmado proveedor |
| 5 | 20 | T. Elimbi | Lateral izquierda (`D`, `2:1`) | Titular confirmado proveedor |
| 6 | 6 | O. Echegini | Banda / Interior derecha (`M`, `3:4`) | Titular confirmado proveedor |
| 7 | 14 | J. Groenen | Mediocentro (`M`, `3:3`) | Titular confirmado proveedor |
| 8 | 30 | E. Østerås | Mediocentro (`M`, `3:2`) | Titular confirmado proveedor |
| 9 | 28 | J. Le Guilly | Banda izquierda (`M`, `3:1`) | Titular confirmado proveedor |
| 10 | 18 | N. Feller | Delantera (`F`, `4:2`) | Titular confirmado proveedor |
| 11 | 19 | M. Kanjinga | Delantera (`F`, `4:1`) | Titular confirmado proveedor |

- **Suplentes Paris Saint Germain W (`12`, `acquisition_record.json`) [`CONFIRMADO` proveedor]:** `O. Szperkowska (#16, G)`, `O. Toussaint (#33, G)`, `P. Dudek (#4, D)`, `Olga Carmona (#77, D)`, `N. Fatier (#23, D)`, `Vitória Yaya (#8, M)`, `F. Jourde (#11, M)`, `G. Le Roux (#27, M)`, `A. Charlier (#34, M)`, `Katia Imarazene (#24, F)`, `M. Lafontaine (#47, F)`, `L. Morissaint (#57, F)`.

---

## 5. Duelos Tácticos y Escenarios de Partido (Actualizado al XI Real `3-4-3` vs `4-4-2`)

1. **Corrección sobre la banda izquierda de PSG y el carril derecho de Chelsea (`CONFIRMADO` en XI real -> hipótesis actualizada):**
   - Frente a la hipótesis inicial que situaba a `Olga Carmona` de salida, PSG alinea por izquierda al dúo **`T. Elimbi (#20)` + `J. Le Guilly (#28)`** (`20` y `23` años). Del lado local, el `3-4-3` proyecta a **`E. Carpenter (#2)`** como carrilera derecha junto a **`W. Kaptein (#19)`** por dentro. Es un duelo de alta exigencia en las ayudas defensivas de `Le Guilly`/`Elimbi` ante las conducciones de `Carpenter` (quien suma goles ante Roma y Charlton), reservando PSG a `Olga Carmona (#77)` en el banco para corregir o refrescar en la segunda mitad.
2. **Eje defensivo de PSG sin Paulina Dudek (`CONFIRMADO` en XI real -> hipótesis actualizada):**
   - La ausencia de `P. Dudek (#4)` en el once titular confirma la pareja **`G. Mbock Bathy (#29)` – `T. Samoura (#2)`** (idéntica a la de Marsella `0-2`). Enfrente, Chelsea sustituye a la lesionada `M. Malard` con **`A. Beever-Jones (#33)`** fijando centrales y **`S. Baltimore (#11)`** atacando desde el perfil izquierdo (con **`A. Thompson (#12)`** como carrilera izquierda de largo recorrido y `K. McCabe (#15)` + `V. Buurman (#5)` cubriendo su espalda junto a `N. Björn (#14)`).
3. **Medular y doble punta móvil `N. Feller`–`M. Kanjinga` en `4-4-2` (`INFERIDO` de cuadrícula API + datos previos):**
   - El cambio de Paulo César a `4-4-2` con **`J. Groenen (#14)`** y **`E. Østerås (#30)`** en doble pivote, **`O. Echegini (#6)`** en derecha y dos atacantes veloces (**`N. Feller (#18)`** y **`M. Kanjinga (#19)`**) sugiere búsqueda de salidas directas en transición aprovechando el espacio tras las carrileras de Chelsea (`Carpenter` y `Thompson`) cuando Chelsea instale posesión alta en campo rival (como en su `65%`–`78%` reciente vs Everton/Häcken).
   - No obstante, la cuadrícula `3-4-3` / `4-4-2` es nominal y no demuestra por sí sola altura de presión; Chelsea puede cerrar en `5-4-1` sin balón o bascular a línea de cuatro con `McCabe` en lateral izquierdo.
4. **Banquillos y margen de ajuste tras el `60'` (`CONFIRMADO` en actas):**
   - Ambos bancos cuentan con piezas de primer nivel listas para alterar el guion: Chelsea retiene en el banquillo a **`L. Bronze (#22)`**, **`K. Buchanan (#26)`**, **`E. Cuthbert (#8)`**, **`S. Nüsken (#6)`**, **`G. Dragoni (#16)`** y **`M. Hamano (#23)`**; PSG dispone de **`Olga Carmona (#77)`**, **`P. Dudek (#4)`**, **`Vitória Yaya (#8)`**, **`F. Jourde (#11)`** y **`M. Lafontaine (#47)`**.
   - El perfil disciplinario arbitral de `Ferrieri Caputi` sigue siendo **INSUFICIENTE** (`n=2` en API).

---

## 6. Puntos Que Quedan Sin Verificar (Actualizado tras `19:34:24+00:00`)

1. **Certificación federativa primaria de UEFA (`primary_official_verified=false`):** Los `22` titulares y `22` suplentes provienen de `API_FOOTBALL / STRUCTURED_PROVIDER` (`19:34:24 UTC`); el once de Chelsea coincide con `chelseafc.com`, mientras el de PSG no se contrastó contra parte HTML directo de UEFA/PSG por ausencia de `chrome_devtools` MCP.
2. **Discrepancia exacta de estadio (`CONTRADICCIÓN`):** `Wheatsheaf Park, Staines` (API) vs `The Cherry Red Records Stadium` (`Kingsmeadow` / `Plough Lane`, fuentes web).
3. **Motivo médico/técnico individual de jugadoras fuera de los `23` de PSG (`NO VERIFICADO`):** `Katoto`, `Earps`, `De Almeida`, `Gaetino`, `Tounkara`, `Albert`, `Dunn`, `Fazer`, `Martens` y `Leuchter` (salvo `Karchaoui`, baja confirmada por tobillo, y `Geyoro`, traspasada).
4. **Muestra arbitral de Maria Sole Ferrieri Caputi (`INSUFICIENTE`):** `n=2` partidos en API (`n=1` en faltas).

---

## Registro de fuentes

### Fuentes locales verificadas por SHA-256 (`outputs/matches/7913441/`)
- **Auditoría de alineaciones (`22` titulares confirmados por proveedor):** `lineups/lineup_audit_7913441.json` — `source_sha256: 962d958a59481c3ecfb82a249994d6c4cd5b0efd3545ce4a082c9bffd31f7655` (`acquired_at_utc: 2026-11-18T19:34:24.616116+00:00`, `API_FOOTBALL / STRUCTURED_PROVIDER`).
- **Contexto diferencial (`4bde42ef...`):**
  - `context_updates/4bde42efcc81581fded11234/acquisition_record.json` — `payload_sha256: 96ecc07dba9a7845362dccb7eecc495abb856ed461db394a2effd77c83c87fa3` (`fetched_at_utc: 2026-11-18T19:34:24.616116+00:00`).
  - `context_updates/4bde42efcc81581fded11234/lineup_delta.json`.
  - `context_updates/4bde42efcc81581fded11234/match_engine_7913441.json` y `match_engine_7913441.txt`.
- **Contexto inicial congelado:**
  - `inputs/match_engine_7913441.json` — `sha256: a963e7bca62a1f23d9c233814167532737e6f611e94a67b865ccac6db04bbddd` (`18:51:10+00:00`).
  - `inputs/match_engine_7913441.txt` — `sha256: 004fa584032248b70b1544f352857e5ffc7cd6897f04a94ce16162f0063926e9`.
  - `inputs/match_identity.json` — `sha256: a748d19dee3d53dd369c452348d46f2da39a9cd1443992056da7be21ef0877e0`.
  - `context_acquisitions/82e4567418fd4d0d56c0517e47f43a0f158af79e461a5619860d00a599f5aed9.json` (`19:16:45+00:00`, `results=0`).

### Fuentes externas consultadas por `search_web` (`2026-11-18`, ~`19:27–19:29 UTC`)
- `[S1]` **chelseafc.com** (XI Chelsea W + banquillo + bajas Bompastor): https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEu0l0qB8egK4o-_5tR-dKOtgCxH39XHkpjBEnevQu0k2111eb6Cm8NmABwujp_chBPKeDQmh_Myq173437UKVDQQ9pO6kpdTynC4b7f24s6cvPMmwQqQvSTszBe1HoSExHW6mBp8lPT3CxTPsy6cYzn_jlgA--YhP2d8_dVQAcmJpStfj7ww==
- `[S2]` **chelseafc.com** (parte médico Sonia Bompastor: Malard, Buchanan, James, Dragoni, Matsukubo): https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQGpa0TlzuAre94FwMrOv5h2RemzAoXla1hhqe8O6Hf7MeaCmf4LqtfyQO4xzzj9F3GEMZVUUfKli90gg_WBRV7gdzowTBlr4zBzTH_o2eUrrX9Z578Z3I4ZJsnuVKoq9NhbNUg1a1ld-mUs6LI3JUquGTMQPPQfg2_M_FSmHyVZaCGAg2wyN2nAFhO_wa24jX8nyFZp67mvl39WnlJ4GZygAgeC9n2c
- `[S3]` **coeursdefoot.fr** (convocatoria de 23 jugadoras del PSG): https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQGVQBbqITjDyzz6CP-SgJFH88B4bv0WH_o3hOknrvd_PGLWaNpHU3c14AmYXS-zb8v_6i0WtyBAjUpZXEQQCiRBYID2s0O8y1W-V8xvr6CHhG5reL7tTBcNSwYZEXVvqJ80XmVlfEXu2eQ-qwpxuZxgEfL-GkGM3hQltkZEBtYq2PTHd_pvPytMYtPceRzi1sKULKzmIe-PLuok
- `[S4]` **coeursdefoot.fr** (baja Sakina Karchaoui por esguince de tobillo 6–7 semanas): https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQHNnCOaClb5Km71XBzy1gTEMEi0uVIVhcfsAKOYjYfy1uGOhhr1EJn4zly6lpZeqqObXuiSBHqyUkYy1hkloZ-uzusQ3KAt4Bdb9ynBs37KyCpTl9fR_QtnkUjAtoyqYanh9vCVy8Dx2F3ABKZAgXNmeOlrO7j2RSVplmGAXzdZ2LtOVfG2g3KM-8RdeM72nb2tQA==
- `[S5]` **psg.fr** (previa oficial de Paulo César, mención a Naomie Feller): https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFVLCM-MKRg3h328cbanW_telepjAOrHkv7VETUxdd915PZdMFVHlDk4c-e2SAuODZ57CDA-rqyk_9s64ADGXSdYculgZjzv7URTSegRV5fyh6leBXmkexb8H9NxsomC_tnlsS_4GSReVpo95IL8NSnA60Konk5nxCRZ-MMpXCxrRB8-8cmBi7yRJhw6DdgCoJDVA8YB30ubxWAFqXhZ5zksKZJ
- `[S6]` **psg.fr** (traspaso permanente de Grace Geyoro a London City Lionesses): https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEKrsz9gf6eGDCIYztiz6CEBMypLlOW12J-NP62XbWBUcgHesCxWFzzFyGM56ZPl1g0MpdJvo5phOagaXUlWgiUwOZ5CVXfMwCblG34Yimvskq35s60D6VkxzKt0vU7lCbMi14=
- `[S7]` **theguardian.com** (banquillo Chelsea W y sede): https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQENZsZVKXjCJcQ0Z1S6qMocwFZCITJwllcIDbSu5vm7j7AUbx77PPJXx_2nQlttW6wfXH1tnVv6oGVirEbdue_jTCXDaKSMYef2cAuYQpQ7EuBdgWTO9ahdirFxibhwP0maP6IjZupTmqHk2nJvOwMRBaYPd26E6Kt7RSO91ojfi-7pug8Iq6sMwgswMAX19OFUv_ytMJZH3ECvIwqt7Ae4UFfR5eT7w-izMk0luDx9cmqeXwcIgnZ_Yg==
- `[S8]` **fotmob.com / vavel.com / jawapos.com** (sede, árbitra y citas de Bompastor):
  - https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQE5cNBq2zC3EY0LOY1oLm_DIEpjO7E8MV1yj1MTCM4sEGSIPy-GfZt-okbWdX9Pfg3vcKzbM_AMNWqrswzwsR3w9QgT5lR_92LSxMvCxhEzQuah58h5uBkI3OfEeqgxWUfMh9xVrx5YRa-dsCqtcUBXbsLhCMpCrpKQvl3U
  - https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQHDWO2IyXXKZ36IZsTIEmLQMH2QRl9tw4Ap-mOO1Sx4VMZ7i_9u6fICq2jJrMjo1lO7HJ0kyCjp1VmaHrAE5x7GBdirESBLJ2l-ghujpxsYgQc61ykjAQri4BcBPWtovrjonnC4e5MA0Upo4cntIk_J-aI8tp28NcnS94yLBqrqaONgTCEKMrgkrNPO_Crq0lK3JpMmy4aDxu67KY0t2CKf2IvMYG4OX-WAv2ix_aiHvKO8Q9OVCn1LKcTflNMck-S4PzhvUQ==
- `[S9]` **spilxperten.com** (meteorología Londres ~9 °C): https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQG0L3CEW1OEG6K7v3vzAxdBCqS0IGtoXQ-VMv148y7ji-7ZOk27EWI4TqQ5Jr3l9Epm494wnFMMXZsE7N5RIO236hz0ESZNOCf79KH4iZ3uRWId_sSo85yBtS39pA_0vf9_1FifCb48nGt-S5pSKVjtbxyqiRDVQeZpHDagDJqf5XWwBPYpc4-r-8piiZb3UWaImcK7ULB2KN5oEA==
