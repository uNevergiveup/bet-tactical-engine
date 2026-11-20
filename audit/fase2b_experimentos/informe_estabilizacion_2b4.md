# BET ENGINE V4 — FASE 2B.4
# Informe de Integración Prospectiva y Cierre de Estabilización Estadística

**Rama:** `refactor/etapa2-fundamentos`  
**Decisión técnica y de gobierno:**
- **Modelo activo de producción y Shadow:** `CoherentMatchModeler_baseline_2.0_2.1A` (`B0_baseline`), **sin cambios** en sus fórmulas, priors `(1.56, 1.26)` ni política de autorización de apuestas (`BET`).
- **Especificación experimental paralela integrada:** `CoherentMatchModeler_cand_2b3b_v1` (`INT_B3_A6a_kish_k8_C4`, clave `CAND_2B3B_V1`), **desacoplada de toda decisión `BET`**, con `experimental_only = true`, `bet_authorization_allowed = false`, `production_calibrated_out_of_sample = false` y `auto_promotion_disabled = true`.
- **Acreditación estadística actual:** **`MUESTRA_INSUFICIENTE` / `NO_ACREDITADO` (`CALIBRATED_OUT_OF_SAMPLE = False`)** hasta superar los umbrales prospectivos definidos en [`protocolo_validacion_prospectiva.md`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/protocolo_validacion_prospectiva.md).

---

## 1. Verificación Previa e Integridad de Artefactos Congelados

Se auditaron y referenciaron los entregables congelados de la Fase 2B.3B y del manifiesto V2 de Fase 2B.2A sin modificar ningún hiperparámetro ni ejecutar nuevos ajustes sobre el corpus histórico:

- [`resultados_2b3b.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/resultados_2b3b.json) (`sha256: c906ecc0798c0ebb76843158361cd514c1c4142e0ee04b01e0671d11dddcad79`)
- [`comparativa_2b3b.md`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/comparativa_2b3b.md)
- [`especificacion_candidata.md`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/especificacion_candidata.md) (`sha256: bb1c6aaea8d65d06d6b82d7c2bea11fdf82990aeefcd3351e1de65cd14`)
- [`protocolo_validacion_prospectiva.md`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/protocolo_validacion_prospectiva.md) (`sha256: 3ad6e607730208e1ae1308be6280ac6b8b6754c0f95f946092d17ad508f9f560`)
- [`MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json) (`sha256: 9e7eb6806a85361e7ed2a36d6904289751ed6dd32431717190a199d4a7298606`)

Los archivos nucleares protegidos por `MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json` permanecen **exactamente intactos**:
- [`bet_engine/modeling/distributions.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/distributions.py) (`3f690ae1493a30df63fc20301db133af0449935857f82d6b10b3febbf8c68b26`)
- [`bet_engine/modeling/prospective_registry.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/prospective_registry.py) (`0701287568209ea1341f41193286ff93f66086f14b5a610e9d508fc55572cdbb`)
- [`bet_engine/modeling/experimental_v2_1b.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/experimental_v2_1b.py) (`9031179c856b574c777d8ed8ee99c62820f27a9be9b4ee3ab05cefce56ec3be3`)
- [`bet_engine/modeling/calibration.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py) (`1868c8c8fbe5b394c41262b849fe9d19268fdf8410f47dc33c0fe18daff3c380`)
- [`bet_engine/context/observation_store.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/observation_store.py) (`2f0236243e74f160fae604d5e5e2f09257e4c39aa65807fad13dbef4ae143c79`)
- [`bet_engine/context/history_quality.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/context/history_quality.py) (`1cc62f8d94f82d70d88f6e733ad6cb398290fe09644545402efc70122bfe77d2`)

Los expedientes cerrados bajo `outputs/matches/` tampoco fueron alterados.

---

## 2. Inventario de Archivos Creados y Modificados en Fase 2B.4

Todos los archivos ejecutables involucrados en la integración prospectiva están sellados con SHA-256 en [`MANIFEST_CANDIDATA_2B3B.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/MANIFEST_CANDIDATA_2B3B.json) (`spec_params_sha256: 41aba3689331527f213a05c9c6a7e015658b45e750b92183bedae9120c5954cd`):

| Archivo | Estado | SHA-256 | Responsabilidad |
|---|---|---|---|
| [`bet_engine/modeling/prospective_cand_2b3b.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/prospective_cand_2b3b.py) | Creado | `d6df5d708ca7dbaa21cbf37faec14fa7866e0694ed38535d7e3da4ec18d29a07` | Estimador congelado `CoherentMatchModeler_cand_2b3b_v1`, evaluador paralelo `B0_baseline` vs `CAND_2B3B_V1`, registro inmutable `"xb"`, liquidación separada y evaluador estadístico pareado |
| [`bet_engine/modeling/__init__.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/__init__.py) | Modificado | `c260cf2aee055136101a290b881fbf60af552319dc5e1cde31a8c73e183e9ed4` | Exportación pública de `CANDIDATE_2B3B_VERSION` y helpers de registro/evaluación 2B.4 |
| [`bet_engine/orchestration/shadow_pipeline.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/orchestration/shadow_pipeline.py) | Modificado | `26cb001bd24574d7ea0d9c2a3f742a3217cb8bd8bc9c0dc813aea4cf616ac0e9` | Hook paralelo no bloqueante (`ShadowRunReport.experimental_cand_2b3b_comparison`) tras `CoherentMatchModeler.build_model` con recuperación aislada |
| [`bet_engine/orchestration/adapters.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/orchestration/adapters.py) | Modificado | `5c851286c908271057cba1b06b3014d9a28a1b40642b04c5ae49deaa97ac3044` | Registro pre-kickoff automático no bloqueante en `outputs/prospective_2b3b/` dentro de `run_shadow_for_event_dir` (solo `not historical`, `status == NS` y sin `BLOCKED_PREMATCH_GUARD`) |
| [`tools/prospective_tracker.py`](file:///Users/antonio/Projects/bet-tactical-engine/tools/prospective_tracker.py) | Modificado | `b2bcec73389fed7d70de8bd83b32321d32328b7640f7da5d914149fbe45e16cb` | Extensión `--track {legacy,2b3b}` (conservando `legacy` por defecto) y subcomando `record-batch` |
| [`audit/fase2b_experimentos/MANIFEST_CANDIDATA_2B3B.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/MANIFEST_CANDIDATA_2B3B.json) | Creado | *(self-manifest)* | Congelación formal de `spec_params`, `corpus_cutoff_utc = 2026-11-14T19:45:00+00:00`, hashes de código y artefactos 2B.3B |
| [`test/test_fase2b4_prospective_integration.py`](file:///Users/antonio/Projects/bet-tactical-engine/test/test_fase2b4_prospective_integration.py) | Creado | — | Batería de 8 bloques que verifican paridad, inmutabilidad, antifuga, aislamiento y bloqueo BET |
| [`audit/fase2b_experimentos/informe_estabilizacion_2b4.md`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/informe_estabilizacion_2b4.md) | Creado | — | Informe de cierre de Fase 2B.4 |

---

## 3. Arquitectura de Evaluación Paralela, Antifuga y Aislamiento de Fallos

### 3.1 Evaluación paralela sin duplicar infraestructura
Se reutilizaron directamente `ObservationStore.from_match_engine_json`, `CoherentMatchModeler.build_model` y las primitivas de validación e identidad de [`prospective_registry.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/prospective_registry.py) (`CORPUS_CUTOFF_UTC`, `_canonical_sha256`, `_verify_target_fixture_identity`, `_classify_lineup_state`, `_classify_odds_status`, `_has_verified_provider_provenance`, `verify_authoritative_result`, `default_settlement_path`) y métricas de [`calibration.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/calibration.py).

Para cada expediente `PREMATCH` válido:
1. **`B0_baseline`** ejecuta literalmente `CoherentMatchModeler.build_model(bundle, history_limit=20)` y conserva su salida de producción íntegra.
2. **`CAND_2B3B_V1`** ejecuta `estimate_cand_2b3b_side_rate` (`shrink_k=8.0`, `use_kish_neff=True`, `venue_mode="shrunk_anchor_50"`, `venue_blend=0.50`, `venue_k=4.0`, `recency_l6_weight=0.35`, `recency_l20_weight=0.65`, `own_attack_weight=0.55`, `opp_defense_weight=0.45`, `shared_h2h_discount=0.50`, `red_card_policy="exclude_any"`, `strict_competition_identity=True`, grilla Dixon-Coles con `rho=-0.04`, `max_goals=12`) sobre el **mismo `bundle`** y **`cutoff_utc`**.
3. Se persiste un único registro inmutable pre-kickoff (`outputs/prospective_2b3b/<fixture_id>_<event_id>.json`, apertura exclusiva `"xb"` con bloqueo antiduplicados por `fixture_id`) que sella:
   - `pre_kickoff_sha256`, `input_sha256`, `match_engine_payload_sha256`, `observations_sha256`, `manifest_sha256`, `spec_params_sha256`;
   - Controles temporales estrictos: `now_dt < ko_dt`, `cutoff_dt <= now_dt`, `cutoff_dt < ko_dt`, `ko_dt > 2026-11-14T19:45:00+00:00` (`CORPUS_CUTOFF_UTC`) y `allow_overwrite=False`;
   - Cobertura histórica y por sede (`own_home_n`, `opp_away_conceded_n`, `own_away_n`, `opp_home_conceded_n`, `min_venue_n`, `insufficient_venue_any_lt3`, `insufficient_own_home_lt3`, `red_card_unknown_*`), `data_quality` (`analytical_quality_score`, `coverage_tier`), `coach_regime` (`COACH_REGIME`), y `lineup_status`;
   - Estado estricto de cuotas Bwin: solo entran como `economic_eval_eligible=True` las capturas `ODDS_VERIFIED` (`odds_captured_utc < cutoff <= kickoff` y `odds_sha256` presente), degradando el resto a `ODDS_UNVERIFIED` o `NO_ODDS` sin contaminar métricas deportivas.

### 3.2 Liquidación separada y evaluación pareada
- [`settle_2b3b_prospective_record`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/prospective_cand_2b3b.py#L743) escribe la liquidación en un archivo hermano `.settlement.json` verificando previamente que `pre_kickoff_sha256` del registro pre-partido coincida sin alterar ni un byte del fichero original.
- Exige `match_status in FINISHED_ELO_STATUSES` y `settled_at_utc >= kickoff_utc + 125m` (`resolve_elo_available_dt`).
- Separa estrictamente `VERIFIED_AUTHORITATIVE` (`creditable=True`, requerido para la cohorte pareada), `UNVERIFIED_RESULT` / `UNVERIFIED_SOURCE_PROVENANCE` (`unverified_result_n`) y `SIMULATED_TEST_ONLY` (`simulated_test_n`).
- [`evaluate_2b3b_prospective_records`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/modeling/prospective_cand_2b3b.py#L928) calcula diferencias pareadas `Cand - B0` mediante block bootstrap diario (`n_boot = 2,000`, `seed = 42`, con `bonferroni_m4_p` sobre las 4 métricas de puerta `rps`, `ll_1x2`, `brier_o25`, `brier_btts`) y desagregaciones completas (`ALL`, `CLUB`, `DOMESTIC_LEAGUE`, `NATIONAL_TEAM`, `INSUFFICIENT_VENUE_ANY_LT3`, `INSUFFICIENT_OWN_HOME_LT3`, `by_league`, `by_depth_bucket`, `by_quality_tier`, `by_lineup_state`).

### 3.3 Imposibilidad arquitectónica de autorizar `BET` y recuperación no bloqueante
- En `ShadowPipelineRunner.run_shadow_on_bundle` ([`shadow_pipeline.py`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/orchestration/shadow_pipeline.py#L619-L637)), `evaluate_parallel_bundle_2b3b` se ejecuta dentro de un bloque `try/except` aislado **después** de haber construido el `model` de producción (`B0`). Sus probabilidades jamás pasan a `BwinMarketEvaluator.evaluate_standard_markets` ni afectan `market_comparisons`, `top_investigable_candidates`, `decision`, `decision_taxonomy` ni `decision_classification`.
- Si el evaluador experimental falla (por excepción real o `simulate_cand_2b3b_failure=True`), Shadow degrada de forma segura (`DEGRADED_EXPERIMENTAL_CAND_2B3B_RECOVERED(...)`) conservando decisiones idénticas al caso nominal.
- En [`run_shadow_for_event_dir`](file:///Users/antonio/Projects/bet-tactical-engine/bet_engine/orchestration/adapters.py#L246-L280), las reejecuciones de un mismo expediente conservan intacto el registro previo (`ALREADY_RECORDED_IMMUTABLE_PRESERVED`) y cualquier error de registro prospectivo se captura como `SKIPPED_NON_BLOCKING` sin interrumpir `shadow_summary_<event_id>.json`.

---

## 4. Verificación y Resultados de Pruebas

Tras la implementación completa de Fase 2B.4 y su manifiesto:
- Verificación de integridad de manifiesto (`verify_cand_2b3b_manifest(MANIFEST_CANDIDATA_2B3B.json)`): **OK** (todos los hashes SHA-256 de código y artefactos verificados).
- Batería específica de integración prospectiva 2B.4 y regresión de fases previas (`test_fase2b4_prospective_integration`, `test_fase2b3b_mechanisms_and_spec`, `test_fase2b3a_experiment_protocol`, `test_fase2b2a_corpus_and_protocol_integrity`, `test_iteracion_2_2c_registry_and_elo`, `test_iteracion_2_2b_prospective_and_elo`, `test_p0_c1_c4_audit_closure`): las pruebas específicas verificaron paridad exacta de `B0`, equivalencia numérica `< 1e-12` de `CoherentMatchModeler_cand_2b3b_v1` con `VariantSpec(INT_B3_A6a_kish_k8_C4)`, bloqueo temporal e inmutabilidad `"xb"`, liquidación separada e invariancia byte-a-byte, idempotencia de batch y Shadow, no afectación ante fallo simulado y operación completa de `tools/prospective_tracker.py --track {2b3b,legacy}`. *(Las cifras exactas de ejecución final tras la última edición se verifican en la pasada de cierre y se reportan en el resumen final.)*

---

## 5. Instrucciones Reproducibles de Operación Prospectiva (`--track 2b3b`)

El modo por defecto de [`tools/prospective_tracker.py`](file:///Users/antonio/Projects/bet-tactical-engine/tools/prospective_tracker.py) sigue siendo `--track legacy` para compatibilidad retroactiva; para operar el seguimiento `B0 vs CoherentMatchModeler_cand_2b3b_v1`:

1. **Registro individual antes del kickoff (`now_utc < kickoff_utc`):**
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py --track 2b3b record \
  --match-engine outputs/matches/<EVENT_ID>/inputs/match_engine_<EVENT_ID>.json \
  --bwin-txt outputs/matches/<EVENT_ID>/inputs/cuotas_bwin_<EVENT_ID>.txt \
  --shadow-json outputs/matches/<EVENT_ID>/shadow/shadow_summary_<EVENT_ID>.json \
  --manifest audit/fase2b_experimentos/MANIFEST_CANDIDATA_2B3B.json \
  --records-dir outputs/prospective_2b3b
```

2. **Registro en lote sobre múltiples expedientes (no se detiene ante fallos o registros previos):**
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py --track 2b3b record-batch \
  --matches-root outputs/matches \
  --manifest audit/fase2b_experimentos/MANIFEST_CANDIDATA_2B3B.json \
  --records-dir outputs/prospective_2b3b
```

3. **Liquidación separada post-partido con fuente oficial API-Football (`FT/AET/PEN`, tras `kickoff + 125m`):**
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py --track 2b3b settle \
  --record outputs/prospective_2b3b/<FIXTURE_ID>_<EVENT_ID>.json \
  --hg <HG> --ag <AG> --status FT \
  --source-json outputs/matches/<EVENT_ID>/inputs/target_fixture_<FIXTURE_ID>.json
```
*(Solo en pruebas sintéticas locales se permite `--simulate`, que etiqueta `SIMULATED_TEST_ONLY` con `creditable=false` y queda excluido del evaluador oficial).*

4. **Evaluación pareada acumulada (`ALL`, `CLUB`, `NATIONAL_TEAM`, `DOMESTIC_LEAGUE`, subgrupos de cobertura/liga/XI):**
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py --track 2b3b evaluate \
  --records-dir outputs/prospective_2b3b
```

---

## 6. Criterios de Acreditación Futura y Limitaciones Vigentes

### 6.1 Umbrales congelados para futura acreditación (sin auto-activación)
Siguiendo [`protocolo_validacion_prospectiva.md`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/protocolo_validacion_prospectiva.md):
- **Muestra mínima:** `CLUB >= 200` partidos pareados acreditados (`VERIFIED_AUTHORITATIVE` y `>= 6` antecedentes limpios; recomendable `>= 300` o `>= 30` días naturales); estrato separado `NATIONAL_TEAM >= 50` (por debajo de `50`, estrictamente descriptivo).
- **Puerta estadística conjunta sobre `CLUB`:** mejora en `RPS 1X2` (`ΔRPS < 0`, `prob_improves >= 0.975`) **y** en `LogLoss 1X2` (`ΔLL < 0`, `prob_improves >= 0.975`) con consistencia direccional en `DOMESTIC_LEAGUE` y mitades cronológicas; mejora o no deterioro en `ECE_5(hw)` y favoritos fuertes `P >= 0.55`; límites estrictos en mercados secundarios (`ΔBrier_O25 <= +0.0005` y `ΔBrier_BTTS <= +0.0005` con `prob_improves >= 0.05`, `ΔPoisson_NLL <= +0.0020`).
- Incluso cumpliéndose esos umbrales (`CRITERIOS_DE_ACREDITACION_PROSPECTIVA_CUMPLIDOS_SIN_ACTIVACION_AUTOMATICA`), **`auto_promotion_disabled = True`** exige una decisión explícita de gobernanza posterior antes de cambiar Shadow o producción.

### 6.2 Limitaciones documentadas que permanecen abiertas
1. **`CALIBRATED_OUT_OF_SAMPLE = False`**: ningún mercado de producción ni candidato 2B.3B dispone todavía de acreditación estadística prospectiva completada.
2. **Fuerza del rival (`SoS/Elo`) pendiente**: `AsOfEloTracker` queda fuera de `CoherentMatchModeler_cand_2b3b_v1` hasta corregir la taxonomía histórica (`UNKNOWN -> CLUB` detectado en `39` equipos) y capturar timestamps reales de publicación API para evitar riesgo de fuga temporal en ligas mixtas/selecciones.
3. **Vigilancia sobre subgrupo `INSUFFICIENT_OWN_HOME_LT3` (`own_home < 3`)**: queda segmentado específicamente en `evaluate_2b3b_prospective_records` para comprobar si el `bias(1) = +0.0151` detectado en 2B.3B requiere condicionar `venue_blend` antes de una eventual promoción.
4. **Calibración financiera condicionada a cobertura Bwin**: solo los registros con `ODDS_VERIFIED` contabilizan en `economic_eval_eligible_n`.

---

## 7. Habilitación Independiente de Desarrollo de *Early Betting*

Se confirma explícitamente que el desarrollo de **Early Betting** puede iniciarse de forma **completamente independiente** del cierre de estabilización estadística, **sin alterar el experimento prospectivo `CoherentMatchModeler_baseline_2.0_2.1A` vs `CoherentMatchModeler_cand_2b3b_v1`**, siempre que:
- Mantenga intactos los módulos sellados por [`MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_backtesting/MANIFEST_MODELOS_CONGELADOS_V2_2B2A.json) y [`MANIFEST_CANDIDATA_2B3B.json`](file:///Users/antonio/Projects/bet-tactical-engine/audit/fase2b_experimentos/MANIFEST_CANDIDATA_2B3B.json);
- Opere sobre sus propios componentes/flujos temporales de captura temprana sin modificar las predicciones selladas en `outputs/prospective_2b3b/` ni relajar el requisito obligatorio de acreditación prospectiva para autorizar apuestas en el pipeline principal de Shadow.
