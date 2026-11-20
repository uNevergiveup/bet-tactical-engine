# BET ENGINE V4 — FASE 2B.3A
# Comparativa Detallada de Experimentos A/B frente al Baseline Congelado

**Fuente única de cifras:** [`audit/fase2b_experimentos/resultados_ab.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/resultados_ab.json)  
**Baseline de referencia:** `CoherentMatchModeler_baseline_2.0_2.1A` (`B0_baseline_frozen`)  
**Criterio de clasificación:** ejecutado exclusivamente en validación walk-forward interna a `train_strict` (`train_wf_main`, `n = 1,447` en 3 folds y 32 bloques diarios; `train_wf_alt_partition`, `n = 1,550` en 36 bloques diarios). La pasada sobre `test_strict` (`n = 2,323`) es confirmatoria única, no de selección.

---

## 1. Resumen Ejecutivo de Clasificación (15 Variantes)

| Variante | Grupo | Veredicto | `ΔRPS WF` (IC95 día) | `ΔLL WF` | `ΔO2.5 WF` | Folds WF (`f1 / f2 / f3`) | `ΔRPS Alt` | `ΔRPS Test*` (confirmatorio) |
|---|---|---|---|---|---|---|---|---|
| `B0_baseline_frozen` | `BASELINE` | `BASELINE` | `0.000000` (`RPS=0.22156`) | `0.000000` (`1.04121`) | `0.000000` (`0.240310`) | `0 / 0 / 0` | `0.000000` | `0.000000` (`RPS=0.21829`) |
| `A1_emp_stratum_priors` | `A_PRIORS` | **`EMPEORA`** | `+0.000397` (`[-0.000217, +0.001277]`) | `+0.000820` | `+0.000398` | `-0.00009 / +0.00009 / +0.00127` | `+0.000264` | `+0.000325` (`[+0.000016, +0.000642]`) |
| `A2_emp_league_priors` | `A_PRIORS` | **`MEJORA_NO_CONCLUYENTE`** | `+0.000236` (`[-0.000505, +0.001057]`) | `-0.000367` | `-0.000661` | `+0.00054 / -0.00041 / +0.00060` | `+0.000196` | `+0.000323` (`[-0.000130, +0.000863]`) |
| `A3_shrink_k_4` | `A_PRIORS` | **`MEJORA_NO_CONCLUYENTE`** | `-0.000286` (`[-0.001061, +0.000294]`) | `-0.001311` | `+0.000033` | `+0.00039 / -0.00110 / -0.00014` | `-0.000363` | `-0.000403` (`[-0.000944, +0.000103]`) |
| `A4_shrink_k_2` | `A_PRIORS` | **`MEJORA_NO_CONCLUYENTE`** | `-0.000027` (`[-0.001479, +0.001074]`) | `-0.000967` | `+0.000512` | `+0.00137 / -0.00164 / +0.00021` | `-0.000181` | `-0.000415` (`[-0.001376, +0.000470]`) |
| `A5_shrink_k_12` | `A_PRIORS` | **`EMPEORA`** | `+0.000447` (`[+0.000122, +0.000858]`) | `+0.001544` | `+0.000233` | `+0.00009 / +0.00087 / +0.00037` | `+0.000484` | `+0.000496` (`[+0.000165, +0.000840]`) |
| `A6_kish_neff_k6` | `A_PRIORS` | **`MEJORA_RESPALDADA`†** | **`-0.000580`** (`[-0.001264, -0.000044]`) | **`-0.002023`** | `-0.000049` | `-0.00025 / -0.00131 / -0.00014` | **`-0.000654`** | `-0.000533` (`[-0.001127, +0.000015]`) |
| `B1_venue_blend_0` | `B_VENUE` | **`EMPEORA`** | `+0.001265` (`[+0.000340, +0.002222]`) | `+0.004393` | `-0.000202` | `-0.00036 / +0.00200 / +0.00226` | `+0.001358` | `+0.002600` (`[+0.001933, +0.003364]`) |
| `B2_venue_blend_50_unshrunk` | `B_VENUE` | **`EMPEORA`** | `+0.000544` (`[-0.000407, +0.001452]`) | `+0.000765` | **`+0.001908`** | `+0.00212 / -0.00012 / -0.00047` | `+0.000434` | `-0.001089` (`[-0.001813, -0.000448]`) |
| `B3_venue_shrunk_50_k4` | `B_VENUE` | **`MEJORA_NO_CONCLUYENTE`** | `-0.000412` (`[-0.001227, +0.000466]`) | `-0.001069` | `-0.000111` | `-0.00065 / -0.00005 / -0.00055` | `-0.000280` | **`-0.001072`** (`[-0.001626, -0.000550]`) |
| `B4_venue_shrunk_70_k4` | `B_VENUE` | **`MEJORA_NO_CONCLUYENTE`** | `-0.000389` (`[-0.001537, +0.000868]`) | `-0.001297` | `+0.000291` | `-0.00012 / -0.00015 / -0.00096` | `-0.000242` | **`-0.001777`** (`[-0.002624, -0.001006]`) |
| `B5_venue_shrunk_80_k4` | `B_VENUE` | **`MEJORA_NO_CONCLUYENTE`** | `-0.000238` (`[-0.001614, +0.001203]`) | `-0.000995` | `+0.000567` | `+0.00028 / -0.00006 / -0.00102` | `-0.000083` | **`-0.001973`** (`[-0.002979, -0.001064]`) |
| `C1_exclude_friendlies_fb4` | `C_COMPETITIONS` | **`EMPEORA`** | `+0.000446` (`[-0.000300, +0.001293]`) | `+0.001314` | `+0.000112` | `+0.00142 / +0.00035 / -0.00053` | `+0.000471` | `+0.000009` (`[-0.000491, +0.000483]`) |
| `C2_exclude_friendlies_strict` | `C_COMPETITIONS` | **`EMPEORA`** | `+0.000352` (`[-0.000399, +0.001234]`) | `+0.001026` | `+0.000264` | `+0.00126 / +0.00023 / -0.00053` | `+0.000474` | `+0.000009` (`[-0.000491, +0.000483]`) |
| `C3_restrict_cups_league_ge6` | `C_COMPETITIONS` | **`MEJORA_NO_CONCLUYENTE`** | `-0.000072` (`[-0.000169, +0.000001]`) | `-0.000014` | `+0.000214` | `-0.00002 / -0.00011 / -0.00009` | `-0.000067` | `+0.000087` (`[-0.000149, +0.000307]`) |
| `C4_strict_comp_identity` | `C_COMPETITIONS` | **`MEJORA_NO_CONCLUYENTE`** | `-0.000014` (`[-0.000251, +0.000251]`) | `-0.000001` | `+0.000066` | `-0.00010 / +0.00008 / -0.00002` | `-0.000014` | `+0.000069` (`[-0.000103, +0.000217]`) |

`†` `A6_kish_neff_k6` satisface todos los criterios pre-registrados para `1X2` (`ΔRPS` y `ΔLL` con IC95 < 0 en `train_wf_main`, 3 folds, semanal y partición alternativa), pero **no corrige ni reduce el sesgo de victoria local ni ECE local** y degrada levemente `Brier BTTS` (ver §2).

---

## 2. Experimento A — Priors y Contracción Bayesiana

### 2.1 Comparativa completa en `train_wf_main` (`n = 1,447`) y `test_strict` (`n = 2,323`)

| Variante | `RPS WF` | `LL 1X2 WF` | `Brier BTTS WF` | `ECE hw WF` | `bias(1) WF` | `bias(λH, λA) WF` | `Fav>=0.55 WF` (`n`) | `str>0.5 WF` (`n`) | `RPS Test` | `ECE hw Test` | `bias(1) Test` | `Fav>=0.55 Test` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `B0_baseline_frozen` | `0.22156` | `1.04121` | `0.245367` | `0.030644` | `-0.0282` | `-0.1170 / -0.0045` | `-0.0324` (`70`) | `-0.0472` (`268`) | `0.21829` | `0.056151` | `-0.0553` | `-0.1342` (`130`) |
| `A1_emp_stratum_priors` | `0.22195` | `1.04203` | `0.246128` | `0.032216` | `-0.0319` | `-0.0928 / +0.0425` | `-0.0267` (`69`) | `-0.0546` (`251`) | `0.21862` | `0.056529` | `-0.0560` | `-0.1301` |
| `A2_emp_league_priors` | `0.22179` | `1.04085` | `0.246014` | `0.031933` | `-0.0313` | `-0.0956 / +0.0335` | `-0.0445` (`83`) | `-0.0485` (`270`) | `0.21862` | `0.056538` | `-0.0556` | `-0.1022` |
| `A3_shrink_k_4` | `0.22127` | `1.03990` | `0.246193` | `0.036816` | `-0.0362` | `-0.1290 / +0.0212` | `-0.0805` (`124`) | `-0.0245` (`288`) | `0.21789` | `0.064271` | `-0.0624` | `-0.1115` |
| `A4_shrink_k_2` | `0.22153` | `1.04025` | `0.247244` | `0.043067` | `-0.0431` | `-0.1393 / +0.0438` | `-0.0583` (`169`) | `-0.0173` (`301`) | `0.21788` | `0.068333` | `-0.0677` | `-0.1056` |
| `A5_shrink_k_12` | `0.22200` | `1.04276` | `0.245069` | `0.034391` | `-0.0235` | `-0.1101 / -0.0190` | `-0.0528` (`51`) | `-0.0619` (`250`) | `0.21879` | `0.054134` | `-0.0507` | `-0.1394` |
| `A6_kish_neff_k6` | **`0.22098`** | **`1.03919`** | `0.245869` | `0.036052` | `-0.0349` | `-0.1272 / +0.0168` | `-0.0858` (`113`) | `-0.0307` (`284`) | **`0.21776`** | `0.062195` | `-0.0609` | `-0.1201` |

### 2.2 Hallazgos clave de Experimento A
1. **Priors empíricos por estrato (`A1`) empeoran el modelo (`EMPEORA`):**
   - Aunque el ajuste global previo al test arroja tasas más altas en `UNKNOWN` `(1.6590, 1.4880)` y `DOMESTIC_CUP` `(1.8275, 1.8063)` respecto a `(1.56, 1.26)`, introducir priors por estrato sin ajustar la mezcla de sede eleva `λ_away` en `+0.0425`, empeora `RPS` en `UNKNOWN` (`0.22506 -> 0.22605`) y `LOW_DEPTH_MIN_6_9` (`0.22003 -> 0.22077`), y daña `RPS` también en `test_strict` (`Δ = +0.000325`, IC95 `[+0.000016, +0.000642]`).
   - `A2_emp_league_priors` alivia puntualmente `UNKNOWN` (`0.22373`) y `O2.5` (`-0.000661`), pero oscila entre folds (`[+0.00054, -0.00041, +0.00060]`) y empeora en `test_strict` (`+0.000323`), quedando en `MEJORA_NO_CONCLUYENTE`.
2. **Aumentar contracción (`A5_shrink_k_12`) empeora (`EMPEORA`):**
   - Confirmación directa de **sobre-contracción**: `ΔRPS WF = +0.000447` (`[+0.000122, +0.000858]`), `ΔLL = +0.001544`, empeoran los 3 folds y empeora `test_strict` (`+0.000496`, `[+0.000165, +0.000840]`).
3. **Reducir `shrink_k` de forma cruda (`A3 k=4`, `A4 k=2`) muestra trade-off estructural:**
   - Reducir `k` desinfla la infrapredicción de diferenciales fuertes en `str > 0.50` (`-0.0472 -> -0.0245` en `A3`, `-0.0173` en `A4`), pero al mantener `venue_blend = 0.25` debilita la asimetría `1.56 vs 1.26` del prior, haciendo que **`bias_hw` empeore** (`-0.0282 -> -0.0362 / -0.0431` en WF; `-0.0553 -> -0.0624 / -0.0677` en test), dispare `ECE_hw` (`0.0431` WF / `0.0683` test) y degrade significativamente `Brier BTTS` (`+0.000826` en `A3`, `+0.001877` en `A4`).
4. **Mecanismo real de `A6_kish_neff_k6` (`MEJORA_RESPALDADA` en 1X2, con salvedad importante):**
   - Es la única variante del estudio que supera todo el filtro pre-registrado de `train_wf` (`ΔRPS WF = -0.000580`, IC95 día `[-0.001264, -0.000044]`, `p<0 = 0.9825`; IC95 semana `[-0.001161, -0.000020]`; `ΔLL = -0.002023`, IC95 `[-0.003922, -0.000437]`; 3/3 folds positivos `[-0.00025, -0.00131, -0.00014]`; partición alternativa `ΔRPS = -0.000654`, `[-0.001278, -0.000092]`; y dirección confirmada en `test_strict` con `ΔRPS = -0.000533`, `[-0.001127, +0.000015]`, `ΔLL = -0.001797`).
   - **Diagnóstico matemático del porqué y sus límites:** En `A6` se combinaron `use_kish_neff=True` y `shrink_k=6.0`. Por desigualdad de Cauchy-Schwarz, `(sum w)^2 / sum(w^2)` es invariante a la escala cuando `w < 1`, elevando el `mean_home_n_eff` de `6.878` (`B0`) a **`8.759`** (`A6`). Eso reduce el peso relativo del prior en historiales con partidos de copa/otros torneos (`w = 0.25 / 0.50`), ayudando particularmente en `LOW_DEPTH_MIN_6_9` (`0.22003 -> 0.21925`). **Sin embargo, `A6` no resuelve la calibración de localía**: empeora `bias_hw` (`-0.0282 -> -0.0349` WF; `-0.0553 -> -0.0609` test), `ECE_hw` (`0.0306 -> 0.0361` WF; `0.0562 -> 0.0622` test) y `Brier BTTS` (`+0.000502`, `[+0.000008, +0.001035]` WF).

---

## 3. Experimento B — Localía y Contracción Bayesiana por Sede

### 3.1 Comparativa completa en `train_wf_main` (`n = 1,447`) y `test_strict` (`n = 2,323`)

| Variante | `RPS WF` | `LL 1X2 WF` | `Brier O2.5 WF` | `Brier BTTS WF` | `ECE hw WF` | `bias(1) WF` | `bias(λH) WF` | `HIGH_VENUE_GAP (RPS / b1)` | `Fav>=0.55 WF` (`n`) | `RPS Test` | `ECE hw Test` | `bias(1) Test` | `Fav>=0.55 Test` |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|
| `B0_baseline_frozen` | `0.22156` | `1.04121` | `0.240310` | `0.245367` | `0.030644` | `-0.0282` | `-0.1170` | `0.22343` / `-0.0186` | `-0.0324` (`70`) | `0.21829` | `0.056151` | `-0.0553` | `-0.1342` (`130`) |
| `B1_venue_blend_0` | `0.22282` | `1.04561` | `0.240108` | `0.244531` | `0.043846` | `-0.0352` | `-0.1405` | `0.22495` / `-0.0293` | `-0.1410` (`19`) | `0.22089` | `0.065349` | `-0.0646` | `-0.1848` |
| `B2_venue_blend_50_unshrunk` | `0.22210` | `1.04198` | `0.242218` | `0.247310` | `0.024847` | `-0.0212` | `-0.0935` | `0.22410` / `-0.0079` | `-0.0198` (`184`) | `0.21720` | `0.047392` | `-0.0460` | `-0.0595` |
| `B3_venue_shrunk_50_k4` | **`0.22114`** | `1.04014` | **`0.240199`** | **`0.245205`** | `0.017507` | `-0.0091` | `-0.0738` | `0.22337` / **`+0.0001`** | `-0.0535` (`101`) | `0.21722` | `0.039541` | `-0.0390` | `-0.0834` |
| `B4_venue_shrunk_70_k4` | `0.22117` | **`1.03992`** | `0.240601` | `0.245695` | `0.007303` | **`+0.0012`** | `-0.0471` | `0.22356` / `+0.0118` | **`-0.0156`** (`157`) | `0.21652` | `0.030024` | `-0.0287` | `-0.0653` |
| `B5_venue_shrunk_80_k4` | `0.22132` | `1.04022` | `0.240877` | `0.245998` | **`0.006535`** | `+0.0064` | `-0.0338` | `0.22381` / `+0.0176` | **`-0.0122`** (`190`) | **`0.21632`** | **`0.024968`** | **`-0.0236`** | **`-0.0554`** |

### 3.2 Hallazgos clave de Experimento B (causa principal de infravaloración local/favoritos)
1. **La localía empírica es indispensable (`B1_venue_blend_0` = `EMPEORA` severo):**
   - Al anular la mezcla de sede, `RPS` sube significativamente tanto en `train_wf` (`+0.001265`, IC95 `[+0.000340, +0.002222]`) como en `test_strict` (`+0.002600`, IC95 `[+0.001933, +0.003364]`), colapsan los favoritos `P >= 0.55` (de `70` a `19`, con sesgo `-0.1410` en WF y `-0.1848` en test) y `str > 0.50` cae de `-0.0472` a `-0.0982`.
2. **Subir peso de sede sin contracción fracasa (`B2_venue_blend_50_unshrunk` = `EMPEORA`):**
   - Con regla `venue_n >= 3` sin regularización, `B2` daña el `fold1` temprano (`+0.00212`), empeora `RPS WF` (`+0.000544`) y rompe significativamente totales (`Brier O2.5 +0.001908`, IC95 `[+0.000805, +0.003143]`; `Brier BTTS +0.001943`, `[+0.000650, +0.003269]`).
3. **La contracción bayesiana por sede (`B3`, `B4`, `B5`) desbloquea calibración local y favoritos — con un límite metodológico en WF temprano:**
   - **`B3_venue_shrunk_50_k4`** neutraliza el daño de `B2` en goles totales (`Brier O2.5 -0.000111`, `Brier BTTS -0.000162` en WF), recorta el sesgo local en un **68 %** (`-0.0282 -> -0.0091`), elimina prácticamente el sesgo en `HIGH_VENUE_GAP` (`-0.0186 -> +0.0001`), reduce `ECE_hw` a casi la mitad (`0.030644 -> 0.017507`), mejora **todos** los folds (`[-0.00065, -0.00005, -0.00055]`), pasa el bootstrap semanal (`[-0.000714, -0.000199]`, `p<0 = 1.000`), mejora también en `RECONSTRUCTED` (`0.22216 -> 0.22038` en test) y logra una mejora fuerte en la pasada bloqueada de `test_strict` (`ΔRPS = -0.001072`, IC95 `[-0.001626, -0.000550]`, `ΔLL = -0.003046`, `Fav bias: -0.1342 -> -0.0834`).
   - Aun así, su clasificación estricta en Fase 2B.3A es **`MEJORA_NO_CONCLUYENTE`** porque en `train_wf_main` (donde hay menos partidos propios de sede por equipo que en otoño) el IC95 diario incluye el cero (`[-0.001227, +0.000466]`, `p<0 = 0.8215`), al igual que en `train_wf_alt_partition` (`[-0.001009, +0.000647]`). Conforme a la disciplina experimental, **no se reclasifica usando `test_strict`**.
   - **`B4_venue_shrunk_70_k4`** lleva el sesgo `bias_hw` prácticamente a cero en WF (`+0.0012`), `ECE_hw` a `0.007303`, y `str > 0.50` a `-0.0064` (`n = 447`), alcanzando en `test_strict` `ΔRPS = -0.001777` (`[-0.002624, -0.001006]`). No obstante, en `train_wf` ya aparecen señales de sobrecarga por sede en `UNKNOWN` (`bias_hw = +0.0219`), `DEPTH_10_19` (`+0.0182`) y una ligera presión en `Brier O2.5` (`+0.000291`, que en `B5` ya es significativa: `+0.000567`, IC95 `[+0.000057, +0.001196]`).

---

## 4. Experimento C — Política de Competiciones e Identidad

| Variante | Subconjunto afectado WF (`n`) | `ΔRPS Global WF` | `ΔRPS Afectados WF` (IC95) | Subconjunto afectado Test (`n`) | `ΔRPS Global Test` | `ΔRPS Afectados Test` (IC95) | Veredicto |
|---|---:|---:|---|---:|---:|---|---|
| `C1_exclude_friendlies_fb4` | `878 / 1,447` | `+0.000446` | `+0.000736` (`[-0.000474, +0.002275]`) | `1,351 / 2,323` | `+0.000009` | `+0.000016` (`[-0.000930, +0.000891]`) | **`EMPEORA`** |
| `C2_exclude_friendlies_strict` | `887 / 1,447` | `+0.000352` | `+0.000574` (`[-0.000640, +0.002034]`) | `1,351 / 2,323` | `+0.000009` | `+0.000016` (`[-0.000930, +0.000891]`) | **`EMPEORA`** |
| `C3_restrict_cups_league_ge6` | `144 / 1,447` | `-0.000072` | `-0.000723` (`[-0.002283, +0.000001]`) | `585 / 2,323` | `+0.000087` | `+0.000347` (`[-0.000608, +0.001265]`) | **`MEJORA_NO_CONCLUYENTE`** |
| `C4_strict_comp_identity` | `173 / 1,447` | `-0.000014` | `-0.000113` (`[-0.002002, +0.002148]`) | `340 / 2,323` | `+0.000069` | `+0.000472` (`[-0.000675, +0.001504]`) | **`MEJORA_NO_CONCLUYENTE`** |

### Hallazgos clave de Experimento C
1. **Excluir amistosos (`C1`, `C2`) empeora la calibración temprana (`EMPEORA`):**
   - En `fold1` (finales de agosto/inicios de septiembre), cuando muchos clubes tienen poca muestra oficial de temporada, eliminar amistosos vacía la profundidad efectiva (`n_eff: 6.878 -> 6.504` en `C1`; `208` partidos de WF caen a `< 6` antecedentes limpios en `C2`) y empeora `fold1` (`+0.00142` en `C1`, `+0.00126` en `C2`). En `fold3`, ya con liga rodada, el efecto se invierte (`-0.00053`), y sobre los `1,239` partidos de WF con `>= 6` no-amistosos `C2` apunta a `-0.000303` (`[-0.000857, +0.000359]`). En `test_strict` el efecto es prácticamente cero (`+0.000009`). Esto explica el mecanismo: **la exclusión incondicional con umbral `< 4` perjudica por pérdida de tamaño muestral al principio de temporada**, mientras que el beneficio potencial de limpiar amistosos solo aparece cuando ya sobra muestra oficial.
2. **Restringir copas domésticas (`C3`) no replica fuera del periodo WF (`MEJORA_NO_CONCLUYENTE`):**
   - Aunque en los `144` partidos modificados de `train_wf` mejora `-0.000723` (con `p<0 = 0.9735` en el límite exacto del 0, `+0.000001`, y daño leve en `O2.5 +0.000214`), en `test_strict` afecta a `585` partidos y **empeora** (`ΔRPS = +0.000347` en afectados, `p<0 = 0.2345`), descartando una mejora generalizable.
3. **Identidad competitiva estricta (`C4`, corrección 2B.2A) tiene impacto empírico nulo sobre RPS (`MEJORA_NO_CONCLUYENTE`):**
   - Modifica `173` encuentros `UNKNOWN` en WF (`ΔRPS = -0.000113` en afectados, `UNKNOWN RPS: 0.22506 -> 0.22496`) y `340` en `test_strict` (`ΔRPS = +0.000472` en afectados). Su valor real es **epistémico/de integridad semántica** (evitar igualar `UNKNOWN == UNKNOWN`), no de calibración estadística directa.

---

## 5. Análisis de Sensibilidad por Ventana (`L6 / L10 / L20`) y Segmentos Críticos

| Variante | `L6` WF (`RPS / b1`) | `L10` WF (`RPS / b1`) | `L20` WF (`RPS / b1`) | `L6` Test (`RPS / b1 / fav`) | `L10` Test (`RPS / b1 / fav`) | `L20` Test (`RPS / b1 / fav`) |
|---|---|---|---|---|---|---|
| `B0_baseline_frozen` | `0.22216 / -0.0249` | `0.22189 / -0.0272` | `0.22156 / -0.0282` | `0.22242 / -0.0456 / -0.0291` | `0.21953 / -0.0488 / -0.0809` | `0.21829 / -0.0553 / -0.1342` |
| `A1_emp_stratum_priors` | `0.22264 / -0.0293` | `0.22228 / -0.0309` | `0.22195 / -0.0319` | `0.22284 / -0.0466 / -0.0710` | `0.21987 / -0.0497 / -0.0892` | `0.21862 / -0.0560 / -0.1301` |
| `A3_shrink_k_4` | `0.22167 / -0.0342` | `0.22160 / -0.0354` | `0.22127 / -0.0362` | `0.22217 / -0.0545 / -0.0372` | `0.21915 / -0.0563 / -0.0752` | `0.21789 / -0.0624 / -0.1115` |
| `B3_venue_shrunk_50_k4` | `0.22170 / -0.0016` | `0.22156 / -0.0077` | **`0.22114`** / `-0.0091` | `0.22125 / -0.0234 / -0.0110` | `0.21855 / -0.0310 / -0.0539` | `0.21722 / -0.0390 / -0.0834` |
| `B4_venue_shrunk_70_k4` | `0.22181 / +0.0094` | `0.22170 / +0.0029` | `0.22117 / +0.0012` | **`0.22106`** / `-0.0122 / +0.0149` | **`0.21810`** / `-0.0202 / -0.0282` | **`0.21652`** / `-0.0287 / -0.0653` |
| `C1_exclude_friendlies_fb4` | `0.22268 / -0.0242` | `0.22247 / -0.0258` | `0.22200 / -0.0268` | `0.22231 / -0.0460 / -0.0407` | `0.21946 / -0.0487 / -0.0860` | `0.21830 / -0.0539 / -0.1217` |
| `C4_strict_comp_identity` | `0.22218 / -0.0247` | `0.22186 / -0.0268` | `0.22154 / -0.0278` | `0.22247 / -0.0456 / -0.0294` | `0.21949 / -0.0486 / -0.0913` | `0.21836 / -0.0548 / -0.1312` |

**Lectura estructural de sensibilidad:**
1. En **todas** las ventanas (`L6`, `L10`, `L20`) y tanto en WF como en `test_strict`, `B3_venue_shrunk_50_k4` y `B4_venue_shrunk_70_k4` mejoran sistemáticamente `RPS` y reducen drásticamente el sesgo local y de favoritos respecto a `B0`.
2. En `B0`, al pasar de `L6` a `L20` en `test_strict`, el sesgo de favoritos `P >= 0.55` empeora progresivamente (`-0.0291` -> `-0.0809` -> `-0.1342`) porque al ampliar la ventana hasta 20 partidos el modelo `B0` diluye aún más la señal específica de sede bajo el `0.75` combinado, mientras `B3`/`B4` contienen esa degradación (`-0.0834` y `-0.0653` en `L20`).
3. En `test_strict`, la separación entre `BOTH_SIDES` y `RECONSTRUCTED` confirma que la mejora de `B3/B4` no es un artefacto de reconstrucción: en `BOTH_SIDES`, `RPS` cae de `0.21816` (`B0`) a `0.21711` (`B3`) y `0.21639` (`B4`), mientras en `RECONSTRUCTED` baja de `0.22216` (`B0`) a `0.22038` (`B3`) y `0.22006` (`B4`). (En `A6_kish_neff_k6`, en cambio, `BOTH_SIDES` mejora `0.21816 -> 0.21756` pero `RECONSTRUCTED` empeora `0.22216 -> 0.22360`).
