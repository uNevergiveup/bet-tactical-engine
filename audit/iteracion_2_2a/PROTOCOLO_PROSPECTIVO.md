# Protocolo de Validación Prospectiva — Iteración 2.2A

**Estado actual de muestra nueva:** **`PENDIENTE_MUESTRA_PROSPECTIVA`** (`0` fixtures finalizados posteriores a `2026-11-14T19:45:00+00:00` encontrados en `outputs/matches/`).  
**Manifiesto congelado:** [MANIFEST_MODELOS_CONGELADOS.json](file:///Users/antonio/Projects/bet-tactical-engine/audit/iteracion_2_2a/MANIFEST_MODELOS_CONGELADOS.json)

---

## 1. Reglas de aislamiento prospectivo

1. **Frontera temporal estricta:** solo son elegibles partidos con `kickoff_utc > 2026-11-14T19:45:00+00:00` (posteriores al último evento del corpus `test_strict` usado en 2.1A/2.1B). Nunca se reutilizan los `2,319` partidos anteriores como holdout nuevo.
2. **Congelación pre-kickoff:** antes del `kickoff_utc` (mínimo `45` minutos antes, alineado con el gate PREMATCH), se calcula y persiste en JSON inmutable la predicción de **todos** los candidatos congelados (`M0`, `M0_plus_Elo`, `M4`, `M4_plus_Elo`) junto con:
   - `sha256` del `match_engine_<event>.json` / bundle de entrada;
   - `sha256` del snapshot de cuotas Bwin y timestamp `odds_captured_utc`;
   - `model_name`, `code_hashes` y parámetros exactos del manifiesto `MANIFEST_MODELOS_CONGELADOS.json`;
   - `elo_home`, `elo_away`, `elo_coverage_ok`, `red_card_unknown_n` observados antes del kickoff.
3. **Resolución post-partido separada:** el resultado oficial (`FT`, `hg`, `ag`) se incorpora **únicamente después de finalizado el encuentro** (`available_at_utc >= kickoff_utc + 125 min`), verificando que la lista de fixtures evaluados sea exactamente la misma para todos los candidatos.
4. **Registro de incidencias:** todo partido con fallo de captura, cambio de horario, suspensión (`PST`/`CANC`), falta de cierre Bwin o falta de historial (`< 6` partidos) se registra en tabla de exclusiones sin borrarlo silenciosamente.

---

## 2. Criterios de tamaño muestral, fecha y decisión fijados por adelantado

- **Tamaño muestral mínimo:**
  - Estrato **`CLUB`**: mínimo **`200` partidos conjuntos** finalizados con cierre válido y evaluación pareada.
  - Estrato **`NATIONAL_TEAM`**: mínimo **`50` partidos** (reportado siempre separado de `CLUB`; si no alcanza `50`, no se extrae inferencia propia para selecciones).
- **Horizonte de corte:** al completar `200` partidos `CLUB` posteriores a `2026-11-14T19:45:00+00:00` o tras `30` días naturales de recolección prospectiva (lo que ocurra después si no se alcanza `N = 200`).
- **Regla de decisión (sin reoptimizar hiperparámetros):**
  - Se compara pareadamente `M0_plus_Elo` y `M4_plus_Elo` frente a `M0` con block bootstrap diario:
  - Para **promover un candidato** a producción se exige: mejora significativa en `1X2 RPS` y `1X2 Log-Loss` (`prob_improves >= 0.975`), **sin deterioro significativo** en `Over 2.5 Brier` ni `BTTS Brier`, y mantenimiento o mejora del sesgo en favoritos y localía.
  - Si `M4_plus_Elo` mantiene deterioro en `Over 2.5` frente a `M0_plus_Elo`, se priorizará `M0_plus_Elo` (por menor riesgo de sobre-dispersión en totales y menor coste computacional) para pasar a la fase de calibración de intervalos y acreditación de familias.
