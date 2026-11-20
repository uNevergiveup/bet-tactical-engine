# BET ENGINE V4 — FASE 2B.3B
# Especificación Candidata Congelada para Validación Prospectiva

**Decisión técnica:** **CONGELAR CANDIDATA ÚNICA PARA VALIDACIÓN PROSPECTIVA INDEPENDIENTE** (manteniendo `CoherentMatchModeler_baseline_2.0_2.1A` intacto en Shadow y producción).

- **Identificador independiente de la especificación:** `CoherentMatchModeler_cand_2b3b_v1` (variante experimental `INT_B3_A6a_kish_k8_C4`).
- **Control de referencia obligatorio:** `CoherentMatchModeler_baseline_2.0_2.1A` (`B0_baseline_frozen`).
- **Estado de acreditación:** `NO_ACREDITADO` / `PENDIENTE_VALIDACION_PROSPECTIVA` (queda terminantemente prohibido emitir `CALIBRATED_OUT_OF_SAMPLE` o activar en producción hasta completar la cohorte prospectiva descrita en [`protocolo_validacion_prospectiva.md`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/protocolo_validacion_prospectiva.md)).

---

## 1. Justificación de la Elección sobre `train_strict` (sin usar `test_strict`)

En `train_wf_main` (`n = 1,447`, 3 folds) y `train_wf_alt` (`n = 1,550`, 3 folds):
1. Separa y combina únicamente los mecanismos que probaron beneficio independiente sin deteriorar mercados secundarios:
   - **`Kish n_eff` con `shrink_k = 8.0`** (`A6a_kish_k8`): aportó el `83 %` de la ganancia de `RPS` de `A6` mediante mayor resolución (`0.005669 -> 0.006237`) sin dañar `BTTS`;
   - **Contracción bayesiana de sede al `50 %` con `venue_k = 4.0`** (`B3_venue_shrunk_50_k4`): redujo el error de calibración `Murphy Reliability` un `61 %` en conjunto (`0.001119 -> 0.000437`), bajó `ECE hw` de `0.030644` a `0.018569` y recortó `bias(1)` de `-0.0282` a `-0.0124` sin producir la degradación significativa en `Over 2.5` (`+0.000403`, `p=0.0125`) ni `BTTS` (`+0.000491`, `p=0.0095`) observada en `B4 (70 %)`;
   - **Identidad competitiva estricta `C4`**: incorporada exclusivamente como corrección de integridad semántica (`UNKNOWN != UNKNOWN`, `league_id > 0` y `season > 0`; impacto neutral demostrado `ΔRPS = -0.000040`, IC95 `[-0.000236, +0.000196]`).
2. Supera pareadamente tanto a `B0` (`ΔRPS = -0.001201`, IC95 día `[-0.002055, -0.000216]`, semana `[-0.001580, -0.000767]`, `alt [-0.001874, -0.000093]`) como a `B3` (`ΔRPS = -0.000789`, `[-0.001230, -0.000355]`) y `A6a` (`ΔRPS = -0.000719`, `-64 %` en `Murphy Reliability`), mejorando en los 3 folds principales (`-0.00139`, `-0.00118`, `-0.00102`) y sin deterioro en `Over 2.5` (`-0.000177`) ni `BTTS` (`-0.000038`).
3. **Por qué aún no se promueve:** bajo ajuste Holm-Bonferroni (`m = 6` variantes integradas) el p-valor bilateral en `RPS` es `0.09995` (`0.05196` en `Poisson NLL`), `SoS/Elo` permanece pendiente y persisten un sesgo positivo en `own_home < 3` (`+0.0151`) y negativo en favoritos fuertes (`-0.0585`). Se requiere confirmación prospectiva intacta.

---

## 2. Fórmula Matemática y Parámetros Exactos de `CoherentMatchModeler_cand_2b3b_v1`

Para un partido objetivo con identificador de liga $L_t$, temporada $S_t$, estrato $E_t$ y fecha de kickoff $T_0$, considerando la familia `goals` (goles FT) con límite histórico `history_limit = 20` y exclusión de distorsión por tarjeta roja (`red_card_distortion == False`, `policy = "exclude_any"`):

### 2.1 Priors por estrato (congelados, sin ajuste empírico)
Idénticos a `B0_baseline_frozen` ([`distributions.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py)):
- `DEFAULT_PRIORS["goals"] = (1.56, 1.26)` (local, visitante);
- `STRATUM_PRIORS`: `DOMESTIC_LEAGUE = (1.56, 1.26)`, `CONTINENTAL_CLUB = (1.54, 1.20)`, `DOMESTIC_CUP = (1.66, 1.32)`, `NATIONAL_TEAM = (1.42, 1.05)`.

### 2.2 Pesos competitivos bajo identidad estricta (`C4`) y deduplicación H2H
Para cada antecedente $m$ en `own_sel` (máximo 20 recientes del equipo propio) y `opp_sel` (máximo 20 recientes del rival):

$$
w_{\text{comp}}(m) =
\begin{cases}
1.00 & \text{si } L_m > 0 \land L_t > 0 \land L_m = L_t \land S_m > 0 \land S_t > 0 \land S_m = S_t \\
0.50 & \text{si } E_m, E_t \notin \{\emptyset, \text{UNKNOWN}, \text{OTHER}\} \land E_m = E_t \\
0.25 & \text{en otro caso}
\end{cases}
$$

Si $m \in \text{own\_sel} \cap \text{opp\_sel}$ (`shared_h2h_ids`), en la rama defensiva del rival (`opp_sel`) el peso para la tasa global ponderada se reduce a la mitad: $w_m \leftarrow 0.50 \cdot w_{\text{comp}}(m)$ (mientras en `own_venue_pairs` / `opp_venue_pairs` conserva $w_{\text{comp}}(m)$).

### 2.3 Tasa base contraída mediante tamaño muestral efectivo de Kish ($k = 8.0$)
Sea $\{(v_i, w_i)\}_{i=1}^n$ la serie ordenada del más reciente al más antiguo:

$$
\bar{v}_{20} = \frac{\sum_{i=1}^n v_i w_i}{\sum_{i=1}^n w_i}, \qquad
\bar{v}_{6} = \frac{\sum_{i=1}^{\min(6,n)} v_i w_i}{\sum_{i=1}^{\min(6,n)} w_i}
$$

$$
\hat{v}_{\text{sample}} =
\begin{cases}
0.35\,\bar{v}_{6} + 0.65\,\bar{v}_{20} & \text{si } n \ge 7 \\
\bar{v}_{20} & \text{si } n \le 6
\end{cases}
$$

$$
n_{\text{eff}}^{\text{Kish}} = \frac{\left(\sum_{i=1}^n w_i\right)^2}{\sum_{i=1}^n w_i^2}, \qquad
\mu_{\text{base}} = \frac{n_{\text{eff}}^{\text{Kish}} \cdot \hat{v}_{\text{sample}} + k \cdot \mu_{\text{prior}}}{n_{\text{eff}}^{\text{Kish}} + k}, \quad k = 8.0
$$

### 2.4 Contracción bayesiana por sede hacia ancla multiplicativa ($w_{\text{venue}} = 0.50$, $k_v = 4.0$)
Sean $\mu_{\text{prior}}$ el prior de la sede objetivo (`own_venue`) y $\mu_{\text{other}}$ el de la sede contraria, con $\mu_{\text{mid}} = \max\!\left(0.25,\; \frac{\mu_{\text{prior}} + \mu_{\text{other}}}{2}\right)$ y multiplicador de sede $m_v = \frac{\mu_{\text{prior}}}{\mu_{\text{mid}}}$:

$$
\text{anchor} = \max(0.15,\; \mu_{\text{base}} \cdot m_v)
$$

Para los antecedentes de la sede correspondiente (`vpairs`, donde $W_v = \sum w_j$ y $\bar{v}_{\text{venue}} = \frac{\sum v_j w_j}{W_v}$ si `vpairs` no es vacío):

$$
\mu_{\text{venue\_shrunk}} =
\begin{cases}
\dfrac{W_v \cdot \bar{v}_{\text{venue}} + k_v \cdot \text{anchor}}{W_v + k_v}, \quad k_v = 4.0 & \text{si } |\text{vpairs}| > 0 \\
\text{anchor} & \text{si } |\text{vpairs}| = 0
\end{cases}
$$

$$
\mu_{\text{final}} = 0.50 \cdot \mu_{\text{base}} + 0.50 \cdot \mu_{\text{venue\_shrunk}}
$$

### 2.5 Tasas esperadas y grilla conjunta Dixon-Coles
Combinando ataque propio (`own_final`) y concesión rival (`opp_final`):

$$
\lambda_{\text{home}} = \max(0.05,\; 0.55 \cdot \mu_{\text{final}}^{\text{home,att}} + 0.45 \cdot \mu_{\text{final}}^{\text{away,def}}), \qquad
\lambda_{\text{away}} = \max(0.05,\; 0.55 \cdot \mu_{\text{final}}^{\text{away,att}} + 0.45 \cdot \mu_{\text{final}}^{\text{home,def}})
$$

La distribución conjunta sobre marcadores $(i, j) \in \{0,\dots,12\}^2$ usa `dixon_coles_joint_grid(lh, la, rho=-0.04, max_goals=12)` sin truncamiento ni clipping probabilístico (`clip_probs=False`).

---

## 3. Hashes Congelados de Código, Especificación y Datos

| Artefacto | SHA-256 verificado | Rol |
|---|---|---|
| [`bet_engine/modeling/distributions.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py) | `3f690ae1493a30df63fc20301db133af0449935857f82d6b10b3febbf8c68b26` | Grilla Dixon-Coles y estimador `B0` congelado |
| [`bet_engine/modeling/experimental_v2_1b.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py) | `9031179c856b574c777d8ed8ee99c62820f27a9be9b4ee3ab05cefce56ec3be3` | Referencia `AsOfEloTracker` / SoS auditado |
| [`bet_engine/context/observation_store.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py) | `2f0236243e74f160fae604d5e5e2f09257e4c39aa65807fad13dbef4ae143c79` | Catálogo de ligas e identidad estricta `C4` |
| [`tools/run_fase2b3a_experiments.py`](file:///Users/antonio/Projects/bet-tactical-engine/tools/run_fase2b3a_experiments.py) | `c3e2c00ab14d8b5e17d1671d51a93afadcbd178253d9fd5bcd71fa5e2ef8544a` | Motor A/B aislado reutilizado por 2B.3B |
| [`tools/run_fase2b3b_experiments.py`](file:///Users/antonio/Projects/bet-tactical-engine/tools/run_fase2b3b_experiments.py) | `7995e7972d85df37f9a88c1c8aa19b1cf7c72815836ba15a70a73b4d54bfb630` | Runner reproducible 2B.3B |
| [`resultados_2b3b.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/resultados_2b3b.json) | `c906ecc0798c0ebb76843158361cd514c1c4142e0ee04b01e0671d11dddcad79` | Salida cuantitativa oficial 2B.3B |
| [`MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json) | `9e7eb6806a85361e7ed2a36d6904289751ed6dd32431717190a199d4a7298606` | Manifiesto V2 + hashes de los 10 archivos fuente del corpus histórico |
