# BET ENGINE V4 — FASE 2B.3B
# Comparativa de Aislamiento de Mecanismos, Integración Controlada y Auditoría de Fuerza del Rival

**Fecha:** `2026-11-19T11:01:59Z`  
**Fuente estructurada única:** [`resultados_2b3b.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/resultados_2b3b.json) (`sha256: c906ecc0798c0ebb76843158361cd514c1c4142e0ee04b01e0671d11dddcad79`)  
**Script reproducible:** [`tools/run_fase2b3b_experiments.py`](file:///Users/antonio/Projects/bet-tactical-engine/tools/run_fase2b3b_experiments.py) (`sha256: 7995e7972d85df37f9a88c1c8aa19b1cf7c72815836ba15a70a73b4d54bfb630`)  
**Política de particiones:** evaluación y selección efectuadas **únicamente dentro de `train_strict`** (`train_wf_main`, `n = 1,447` en 3 folds y 32 bloques diarios; `train_wf_alt`, `n = 1,550` en 3 folds y 36 bloques diarios). `test_strict` (`n = 2,323`) queda excluido de optimización como `DIAGNOSTIC_INSPECTED`.

---

## 1. Verificación Inicial e Integridad Referencial

- **Modelos y código base intactos:** [`distributions.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py) (`MODEL_VERSION = CoherentMatchModeler_baseline_2.0_2.1A`, `sha256: 3f690ae1493a30df63fc20301db133af0449935857f82d6b10b3febbf8c68b26`), [`experimental_v2_1b.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py) (`sha256: 9031179c856b574c777d8ed8ee99c62820f27a9be9b4ee3ab05cefce56ec3be3`), [`observation_store.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py) (`sha256: 2f0236243e74f160fae604d5e5e2f09257e4c39aa65807fad13dbef4ae143c79`), [`MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json) (`sha256: 9e7eb6806a85361e7ed2a36d6904289751ed6dd32431717190a199d4a7298606`) y [`resultados_ab.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/resultados_ab.json) (`sha256: 52b620018d6133f38b59084d3144875b69ce090f425260a7d811c68a5a02a21a`).
- **Paridad exacta `B0`:** diferencia máxima absoluta frente a `_estimate_team_rate_detailed` en 200 fixtures (`HOME` y `AWAY`) = `0.0` (`< 1e-12`).
- **Población idéntica en todas las variantes:** `4,496` elegibles PREMATCH en corpus con catálogo de `263` ligas (`2,173` en `train_strict`, `1,447` en `train_wf_main`, `1,550` en `train_wf_alt`).

---

## 2. Experimento A6 — Descomposición 2x2 (`Kish n_eff` vs `shrink_k=6`)

Evaluación estrictamente pareada sobre los mismos `1,447` encuentros de `train_wf_main` (`B0` vs componentes aislados):

| Variante | Fórmula `n_eff` / `k` | `RPS WF` (`ΔRPS` IC95 día) | `LL 1X2` | `Brier 1X2` | `Rel. 1X2` | `Res. 1X2` | `Brier O2.5` | `Brier BTTS` | `Poisson NLL` | `ECE hw` / `bias(1)` | `Fav>=0.55` (`n`) | `n_eff` medio |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|
| `B0_baseline_frozen` | `sum(w)`, `k=8` | `0.22156` (ref) | `1.04121` | `0.20887` | `0.001119` | `0.005669` | `0.240310` | `0.245367` | `3.12036` | `0.030644 / -0.0282` | `-0.0324` (`70`) | `6.878` |
| `A6a_kish_k8` | **`Kish`**, `k=8` | **`0.22108`** (`-0.000481`, `[-0.000920, -0.000095]`) | `1.03967` | `0.20851` | `0.001215` | `0.006237` | `0.240212` | `0.245504` | `3.11684` | `0.032362 / -0.0315` | `-0.0725` (`90`) | `8.759` |
| `A6b_sumw_k6` | `sum(w)`, **`k=6`** | `0.22136` (`-0.000197`, `[-0.000509, +0.000047]`) | `1.04044` | `0.20870` | `0.001050` | `0.005906` | `0.240256` | `0.245666` | `3.11836` | `0.031572 / -0.0316` | `-0.0635` (`91`) | `6.878` |
| `A6_kish_k6` | **`Kish`**, **`k=6`** | `0.22098` (`-0.000580`, `[-0.001264, -0.000044]`) | `1.03919` | `0.20840` | `0.001243` | `0.006652` | `0.240261` | **`0.245869`** | `3.11564` | `0.036052 / -0.0349` | `-0.0858` (`113`) | `8.759` |

### Contrastes pareados directos entre componentes de A6
- **`A6_kish_k6` vs `A6a_kish_k8` (efecto incremental de bajar `k: 8 -> 6` dado Kish):**
  - `ΔRPS = -0.000099`, IC95 `[-0.000422, +0.000147]` (`p_imp = 0.7665`, **no significativo**); `ΔRPS_alt = -0.000133` (`[-0.000422, +0.000118]`);
  - **Deterioro significativo en `BTTS`:** `ΔBrier_BTTS = +0.000366`, IC95 `[+0.000143, +0.000584]` (`p_improves = 0.0000`);
  - Agrava `ECE_hw` (`0.03236 -> 0.03605`), `bias(1)` (`-0.0315 -> -0.0349`), `bias(λ_home)` (`-0.1221 -> -0.1272`) y el sesgo en favoritos fuertes `P>=0.55` (`-0.0725 -> -0.0858`).
- **`A6_kish_k6` vs `A6b_sumw_k6` (efecto incremental de activar Kish dado `k=6`):**
  - `ΔRPS = -0.000384` (`[-0.000838, -0.000001]`, `p_imp = 0.975`), `ΔLL = -0.001255` (`[-0.002489, -0.000117]`), `ΔPoisson_NLL = -0.002719` (`[-0.005583, -0.000380]`), `ΔRPS_alt = -0.000427` (`[-0.000848, -0.000025]`).

**Conclusión de mecanismo (Sección 2):** **`A6a_kish_k8` explica el `83 %` de la reducción de `RPS`** (`-0.000481 / -0.000580`), con intervalo de confianza más estrecho y significativo tanto en bloques diarios (`[-0.000920, -0.000095]`) como semanales (`[-0.000821, -0.000093]`) y partición alternativa (`[-0.000924, -0.000127]`), impulsado por mayor **Resolución** (`0.005669 -> 0.006237`) al evitar la doble penalización del `sum(w)` cuando el equipo acumula historial con pesos `< 1.0`. Bajar `shrink_k` a `6` es secundario y daña `BTTS`. Ningún subcomponente de `A6` corrige el sesgo local (`bias_hw` pasa de `-0.0282` a `-0.0315 / -0.0349`).

---

## 3. Experimento de Localía (`B0`, `B3 50% k=4`, `B4 70% k=4`)

### 3.1 Calibración vs Puntuación Predictiva (`train_wf_main`, `n = 1,447`)

Nota metodológica: la descomposición de Murphy en 5 bins (`Rel.` y `Res.`) evalúa el error de calibración por cuantiles discretos y la discriminación entre bins junto a las métricas continuas exactas (`RPS`, `LogLoss`, `Brier`, `Poisson NLL`, `RMSE`).

| Métrica | `B0` (25% unshrunk) | `B3` (50%, `k_v=4`) | `B4` (70%, `k_v=4`) | Lectura `B4 vs B3` (IC95 día) |
|---|---:|---:|---:|---|
| `RPS 1X2` (`Δ vs B0`) | `0.22156` | **`0.22114`** (`-0.000412`, `[-0.001227, +0.000466]`) | `0.22117` (`-0.000389`, `[-0.001537, +0.000868]`) | `+0.000023` (`[-0.000458, +0.000511]`, p=0.45) |
| `LogLoss 1X2` | `1.04121` | `1.04014` | **`1.03992`** | `-0.000228` (`[-0.001575, +0.001266]`, p=0.62) |
| `Brier 1X2` (medio) | `0.20887` | `0.20863` | **`0.20859`** | `-0.000033` (`[-0.000381, +0.000304]`) |
| `Murphy Reliability 1X2` | `0.001119` | `0.000459` (**-59 %**) | **`0.000225`** (**-80 %**) | Gran mejora de calibración en `B4` |
| `Murphy Resolution 1X2` | **`0.005669`** | `0.004332` | `0.004071` | `B3/B4` reducen dispersión entre bins |
| `ECE hw` / `ECE aw` | `0.03064 / 0.03326` | `0.01751 / 0.01454` | **`0.00730 / 0.00715`** | Calibración marginal casi perfecta en `B4` |
| `bias(1)` / `bias(X)` / `bias(2)` | `-0.0282 / +0.0090 / +0.0192` | `-0.0091 / +0.0077 / +0.0015` | **`+0.0012 / +0.0059 / -0.0071`** | `B3` neutraliza visitante; `B4` invierte ligeramente |
| `GOALS_FT`: `bias(λH, λA)` | `-0.1170 / -0.0045` | `-0.0738 / -0.0452` | `-0.0471 / -0.0638` | `bias(λ_total)`: `-0.1216 -> -0.1190 -> -0.1109` |
| `RMSE_total` / `Poisson NLL` | `1.79605 / 3.12036` | `1.79578` / **`3.11887`** | `1.79576 / 3.11938` | `B3` maximiza verosimilitud Poisson total |
| `Brier Over 2.5` (`bias`) | `0.240310` (`-0.0187`) | **`0.240199`** (`-0.0179`) | `0.240601` (`-0.0169`) | **`+0.000403` (`[+0.000053, +0.000843]`, `p=0.0125`) — `B4` empeora** |
| `Brier BTTS` (`bias`) | `0.245367` (`+0.0085`) | **`0.245205`** (`+0.0073`) | `0.245695` (`+0.0050`) | **`+0.000491` (`[+0.000082, +0.000897]`, `p=0.0095`) — `B4` empeora** |

### 3.2 Estabilidad entre folds y partición alternativa (`B0 / B3 / B4`)
- **Folds principales (`f1 / f2 / f3`, `RPS` y `bias(1)`):**
  - `B0`: `0.22309 (-0.0346)` / `0.22268 (-0.0247)` / `0.21860 (-0.0249)`
  - `B3`: **`0.22244`** (`-0.0143`) / `0.22263` (`-0.0061`) / `0.21805` (`-0.0067`) — **mejora los 3 folds**; `Brier O2.5` mejora en 2/3 y empata f1 (`0.23709 vs 0.23703`).
  - `B4`: `0.22297 (-0.0040)` / `0.22253 (+0.0044)` / **`0.21764`** (`+0.0036`) — mejora f3, casi plano f1, pero `O2.5` empeora en los 3 folds (`0.23717 / 0.23880 / 0.24643`).
- **Partición alternativa (`alt1 / alt2 / alt3`, `RPS` pooled `1,550`):** `B0 = 0.22148`, `B3 = 0.22120` (`-0.000280`, `[-0.001009, +0.000647]`), `B4 = 0.22124` (`-0.000242`, `[-0.001340, +0.001072]`).

### 3.3 Subgrupos críticos de localía: favoritos, muestras pequeñas, localía insuficiente y competiciones

| Subgrupo (`n` en WF main) | `B0` (`RPS / bias(1) / O2.5`) | `B3` (`RPS / bias(1) / O2.5`) | `B4` (`RPS / bias(1) / O2.5`) |
|---|---|---|---|
| **Favoritos fuertes `P >= 0.55`** (`n` varía: `70 / 101 / 157`) | bias comb.: `-0.0324` (`60` H / `10` A) | bias comb.: `-0.0535` (`94` H / `7` A) | bias comb.: **`-0.0156`** (`150` H / `7` A) |
| **Diferencial `λ_H - λ_A > 0.50`** (`n: 268 / 357 / 447`) | `bias(1): -0.0472` | `bias(1): -0.0240` | `bias(1):` **`-0.0064`** |
| **Favoritos débiles `0.45 <= P < 0.55`** (`n: 480 / 549 / 586`) | `bias comb.: -0.0600` | `bias comb.: -0.0337` | `bias comb.:` **`-0.0235`** |
| **Partidos igualados `P_max < 0.45`** (`n: 897 / 797 / 704`) | **`RPS: 0.22802`** (`b1: -0.0236`) | `RPS: 0.22943` (`b1: +0.0075`) | `RPS: 0.23128` (`b1: +0.0187`) |
| **Muestra pequeña `6..9` (`n = 698`)** | `0.22003 / -0.0481 / 0.238786` | `0.21921 / -0.0279 / 0.239042` | **`0.21875`** `/ -0.0169 / 0.239106` |
| **Muestra media `10..14` (`n = 715`)** | `0.22399 / -0.0176 / 0.241497` | **`0.22387`** `/ +0.0004 / 0.241008` | `0.22428 / +0.0104 / 0.241723` |
| **Localía SUFICIENTE `all venue >= 3` (`n = 1,308`)** | `0.22142 / -0.0321 / 0.239949` | `0.22096 / -0.0135 / 0.239898` | **`0.22088`** `/ -0.0031 / 0.240314` |
| **Localía INSUFICIENTE `any venue < 3` (`n = 139`)** | **`0.22280`** `/ +0.0084 / 0.243706` | `0.22285 / +0.0323 / 0.243030` | `0.22386 / +0.0423 / 0.243307` |
| **Propio local INSUFICIENTE `own_home < 3` (`n = 68`)** | **`0.21139`** `/ -0.0090 / 0.232848` | `0.21429 / +0.0157 / 0.230175` | `0.21459 / +0.0245 / 0.228514` |
| **`DOMESTIC_LEAGUE` (`n = 1,128`)** | `0.22017 / -0.0268 / 0.238417` | **`0.21964`** `/ -0.0083 / 0.238381` | `0.21970 / +0.0021 / 0.238771` |
| **`UNKNOWN` (`n = 199`)** | `0.22506 / -0.0078 / 0.250640` | **`0.22451`** `/ +0.0123 / 0.250223` | `0.22520 / +0.0219 / 0.250807` |
| **`DOMESTIC_CUP` (`n = 94`)** | **`0.22780`** `/ -0.0663 / 0.244148` | `0.22953 / -0.0428 / 0.243223` | `0.22838 / -0.0317 / 0.243534` |

**Distinción Calibración vs Puntuación (Sección 3):** **`B4` (`70 %`) logra la mejor calibración marginal (`ECE hw = 0.0073`, `Murphy Rel = 0.000225`), pero NO mejora la puntuación predictiva global frente a `B3` (`50 %`)**: empata en `RPS 1X2` (`+0.000023`), empeora **estadísticamente** `Over 2.5` (`+0.000403`, `p=0.0125`) y `BTTS` (`+0.000491`, `p=0.0095`), sobrepredice la victoria local cuando falta historial en sede (`own_home < 3`: `bias(1) +0.0245`, `RPS 0.21459` vs `B0 0.21139`) y en partidos igualados/estrato `UNKNOWN`. **`B3` representa el equilibrio sin penalización en totales**, justificando pasar **solo `B3`** a la integración controlada con `A6a`.

---

## 4. Integración Controlada (`B3` + Componentes de `A6` + Integridad `C4`)

Todos los modelos integrados se evalúan contra `B0`, entre sí y contra sus componentes individuales (`B3`, `A6a`, `A6b`, `A6`, sin `C4`), aplicando ajuste de multiplicidad **Holm-Bonferroni** sobre la familia de `m = 6` variantes integradas:

| Variante | `RPS WF` (`ΔRPS` IC95 día) | `ΔLL 1X2` | `Rel. / Res.` | `Brier O2.5` | `Brier BTTS` | `Poisson NLL` | `ECE hw / b(1)` | `RPS Alt` (`IC95`) | `Holm RPS (m=6)` |
|---|---|---:|---|---:|---:|---:|---|---|---:|
| `B0_baseline_frozen` | `0.22156` (ref) | `1.04121` | `0.001119 / 0.005669` | `0.240310` | `0.245367` | `3.12036` | `0.0306 / -0.0282` | `0.22148` | — |
| `C4_strict_comp_identity` | `0.22154` (`-0.000014`, `[-0.000251, +0.000251]`) | `-0.000001` | `0.001191 / 0.005891` | `0.240376` | `0.245380` | `3.12081` | `0.0335 / -0.0278` | `0.22147` | — |
| `INT_B3_C4_only` | `0.22118` (`-0.000375`, `[-0.001265, +0.000681]`) | `-0.000927` | `0.000440 / 0.004200` | `0.240227` | `0.245192` | `3.11934` | `0.0171 / -0.0086` | `0.22125` | `0.46477` |
| `INT_B3_A6b_k6` | `0.22077` (`-0.000783`, `[-0.001486, -0.000053]`) | `-0.002305` | `0.000446 / 0.005215` | `0.240134` | `0.245506` | `3.11616` | `0.0228 / -0.0127` | `0.22080` (`[-0.001319, +0.000086]`) | `0.10794` |
| `INT_B3_A6b_k6_C4` | `0.22080` (`-0.000760`, `[-0.001550, +0.000137]`) | `-0.002202` | `0.000427 / 0.005103` | `0.240159` | `0.245493` | `3.11657` | `0.0214 / -0.0122` | `0.22083` (`[-0.001378, +0.000251]`) | `0.18790` |
| `INT_B3_A6a_kish_k8` | `0.22040` (`-0.001160`, `[-0.001988, -0.000215]`) | `-0.003314` | `0.000487 / 0.005042` | **`0.240072`** | `0.245330` | `3.11414` | `0.0212 / -0.0129` | `0.22041` (`[-0.001850, -0.000101]`) | `0.09995` |
| **`INT_B3_A6a_kish_k8_C4`** | **`0.22036`** (`-0.001201`, `[-0.002055, -0.000216]`) | **`-0.003397`** | `0.000437 / 0.004799` | `0.240133` | **`0.245328`** | **`3.11447`** | `0.0186 / -0.0124` | `0.22038` (`[-0.001874, -0.000093]`) | `0.09995` |
| `INT_B3_A6_kish_k6_C4` | `0.22008` (`-0.001482`, `[-0.002310, -0.000549]`) | `-0.004367` | `0.000478 / 0.005543` | `0.240193` | **`0.245705`** | `3.11262` | `0.0179 / -0.0160` | `0.22006` (`[-0.002171, -0.000507]`) | **`0.03000`** |

### Evaluación frente a componentes individuales y papel exacto de `C4`
1. **Sinergia demostrada de `INT_B3_A6a_kish_k8_C4` frente a sus dos pilares:**
   - **Vs `B3_venue_shrunk_50_k4` solo:** `ΔRPS = -0.000789` (`[-0.001230, -0.000355]`, `p_imp = 0.999`), `ΔLL = -0.002328` (`[-0.003583, -0.001027]`, `p_imp = 0.9995`), `ΔPoisson_NLL = -0.004403` (`[-0.007547, -0.001377]`, `p_imp = 0.998`), `ΔRPS_alt = -0.000821` (`[-0.001225, -0.000363]`, `p_imp = 1.000`), con mejora simultánea en `O2.5` (`-0.000066`) y sin daño significativo en `BTTS` (`+0.000124`, `p=0.246`). En `DOMESTIC_CUP`, donde `B3` empeoraba (`0.22953`), `INT_B3_A6a_kish_k8_C4` mejora claramente sobre `B0` (`0.22780 -> 0.22460`).
   - **Vs `A6a_kish_k8` solo:** `ΔRPS = -0.000719` (`[-0.001548, +0.000282]`, `p_imp = 0.927`), `ΔLL = -0.001858`, reduce el error de calibración `Murphy Reliability` un **`64 %`** (`0.001215 -> 0.000437`), recorta `ECE_hw` un **`43 %`** (`0.03236 -> 0.01857`), reduce `bias(1)` un **`61 %`** (`-0.0315 -> -0.0124`) y mejora tanto `O2.5` (`0.240212 -> 0.240133`) como `BTTS` (`0.245504 -> 0.245328`).
   - **Consistencia de folds (`INT_B3_A6a_kish_k8_C4` vs `B0`):** mejora en **3/3 folds principales** (`fold1: 0.22309 -> 0.22170`; `fold2: 0.22268 -> 0.22150`; `fold3: 0.21860 -> 0.21758`), **3/3 folds alternativos** (`alt1: 0.22150 -> 0.22056`; `alt2: 0.22310 -> 0.22112`; `alt3: 0.21995 -> 0.21953`) y **IC95 semanal `[-0.001580, -0.000767]` (`p_imp = 1.000`)**.
2. **Aislamiento de `C4` como corrección estricta de integridad:**
   - Comparar `INT_B3_A6a_kish_k8_C4` frente a `INT_B3_A6a_kish_k8` arroja `ΔRPS = -0.000040` (`[-0.000236, +0.000196]`, `p_imp = 0.6455`), `ΔLL = -0.000083`, `ΔRPS_alt = -0.000031`. Análogamente, `INT_B3_C4_only` vs `B3` arroja `+0.000037` (`[-0.000198, +0.000342]`). **No se atribuye ventaja predictiva a `C4`**: se incluye exclusivamente para impedir la equiparación errónea `UNKNOWN == UNKNOWN` y IDs `<= 0`.
3. **Por qué se descarta `INT_B3_A6_kish_k6_C4` frente a `INT_B3_A6a_kish_k8_C4`:**
   - Aunque `k=6` logra menor `RPS` puntual (`0.22008`, `Δ vs k=8 = -0.000281`, `[-0.000617, -0.000020]`), **degrada significativamente `Brier BTTS` frente a `k=8`** (`+0.000377`, IC95 `[+0.000145, +0.000605]`, `p_improves = 0.0005`), deja `BTTS` peor que `B0` (`0.245705` vs `0.245367`), empeora `bias(1)` (`-0.0124 -> -0.0160`), `Murphy Reliability` (`0.000437 -> 0.000478`) y amplifica el sesgo negativo cuando falta profundidad.

---

## 5. Auditoría de Fuerza del Rival (`SoS` y `Elo`) — Clasificación: `PENDIENTE`

Conforme a los cinco criterios obligatorios previos a cualquier ajuste:

| Criterio de Auditoría | `SoS` (`use_sos_opp_rates`) | `Elo` (`AsOfEloTracker`) |
|---|---|---|
| **1. Disponibilidad temporal** | `100 %` sintético (`avail = ko + 125m`); cero timestamps reales verificados (`VERIFIED_TIMESTAMP = 0`). | `23,063/23,063` con `ESTIMATED_LEGACY_125M` (`0` timestamps verificados); `24` huecos secuenciales `<= 180m` (en su mayoría `Club Friendlies`), aunque el desplazamiento `+125m -> +240m` produce `0/1,447` diferencias en `wf_main`. |
| **2. Cálculo as-of sin fuga** | Depende de listas locales `team_ordered_by_id` con `available_at_utc <= m_ko`. | Garantizado por búsqueda binaria `avail_dt <= target_ko_dt` y deduplicación por `fixture_id`. |
| **3. Estabilidad de identidades y competiciones** | Mezcla rivales con distinta cobertura de catálogo y sin ajuste de densidad de liga. | **Fallo estructural:** `_stratum_key` colapsa todo salvo `NATIONAL_TEAM` a `CLUB` (`3,177` partidos `unknown_stratum` tras catálogo, `4,912` brutos). **`39` equipos dividen su historial entre `CLUB` y `NATIONAL_TEAM`** (ej. `team_id=9` España con clasificatorios `league_id=32` tratados como `CLUB`). El grafo `CLUB` tiene **`48` componentes desconectadas** (`69` sin `1,909` amistosos), todos arrancando en `1500.0` y con amistosos ponderados a `k=20`. |
| **4. Equipos sin historial** | Devuelve tasa bruta del partido sin ajuste de rival (`adj_v = raw_v`). | Devuelve `(1500.0, n < 3)` con `elo_coverage_ok = False`. |
| **5. Cobertura en `train_wf_main`** | **Insuficiente: `12,671 / 29,899` (`42.38 %`)** pares rival-partido con `>= 3` antecedentes (`> 57 %` sin ajuste). | `1,447 / 1,447` (`100 %`, `min = 4`, media `11.03`), sostenida únicamente por el filtro retrospectivo `>= 6` antecedentes del corpus y coeficientes heredados (`k=20, hfa=55, elo_weight=0.25`) aún no validados conjuntamente sobre `B3 + Kish`. |

**Veredicto Sección 5:** **NO implementar `SoS` ni `Elo` en esta fase** (`audit_status_sos = PENDIENTE_COBERTURA_INSUFICIENTE`, `audit_status_elo = PENDIENTE_POR_IDENTIDAD_TIMESTAMPS_Y_DESCONEXION`). No se introducen coeficientes arbitrarios de fuerza del rival.

---

## 6. Síntesis de Métricas Empeoradas, Limitaciones e Incertidumbres

1. **Multiplicidad en bloque de integración (`m = 6`):** aunque `INT_B3_A6a_kish_k8_C4` muestra IC95 diarios (`[-0.002055, -0.000216]`), semanales (`[-0.001580, -0.000767]`) y en partición alternativa (`[-0.001874, -0.000093]`) estrictamente favorables con `p_two_sided = 0.01999`, su valor p ajustado por **Holm-Bonferroni (`m = 6`) es `0.09995` en `RPS` (`0.10995` en `LL 1X2`, `0.05196` en `Poisson NLL`)**. Por rigor estadístico, esto respalda **congelar la especificación solo como candidata a validación prospectiva independiente**, no declarar mejora definitiva ni activar producción.
2. **Incertidumbre con localía insuficiente (`own_home < 3`, `n = 68`):** bajo `B3` / `INT_B3_A6a_kish_k8_C4`, cuando el equipo local tiene menos de 3 partidos en casa, el ancla multiplicativa fija `venue_k = 4` induce un ligero sesgo positivo (`bias(1) +0.0151`, `RPS 0.21406` vs `B0 0.21139`), aunque mejora `O2.5` y `BTTS` en ese subgrupo.
3. **Sesgo residual en favoritos fuertes (`-0.0585` en `P >= 0.55`, `-0.1037` en `λ diff < -0.50`):** coherente con la ausencia aún pendiente de un ajuste limpio por fuerza del rival (`SoS/Elo`).
4. **Cobertura muestral pequeña en estratos minoritarios:** `NATIONAL_TEAM` (`n = 8`) y `CONTINENTAL_CLUB` (`n = 18`) tienen tamaño insuficiente para extraer conclusiones independientes en `train_wf_main`.
