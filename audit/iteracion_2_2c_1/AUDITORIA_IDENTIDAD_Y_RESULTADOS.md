# Auditoría de Identidad, Resultados, Archivos y Cuotas (Iteración 2.2C.1)

## 1. Verificación de identidad del fixture (`_verify_target_fixture_identity`)
Antes de etiquetar `PREMATCH_VERIFIED`, `record_prospective_prediction` contrasta `match_engine_payload["fixture"]` y `ObservationStore` contra `context.target_fixture_full.response[0]`:
- `fixture_id` coincidente.
- `home_id` y `away_id` coincidentes.
- `league_id` y `season` coincidentes (cuando constan).
- Diferencia entre fechas/kickoff `<= 60` segundos.
- Estado `status.short` estrictamente en `ALLOWED_PREMATCH_STATUSES = {"NS"}` (rechaza `TBD`, `UNKNOWN`, vacío, en vivo o finalizados).

```python
# bet_engine/modeling/prospective_registry.py (_verify_target_fixture_identity)
t_status = str((t_fx.get("status") or {}).get("short") or "").strip().upper()
if t_status in LIVE_OR_FINISHED_STATUSES:
    raise ProspectiveRegistryError(f"RETROSPECTIVE_RECORD_FORBIDDEN:status={t_status}")
if t_status not in ALLOWED_PREMATCH_STATUSES:
    raise ProspectiveRegistryError(f"UNKNOWN_OR_NON_PREMATCH_STATUS:{t_status or 'EMPTY'}")
```

## 2. Separación de resultados autorizados (`settle_prospective_record`)
Se separan explícitamente tres conceptos:
1. **Coincidencia de campos (`verify_authoritative_result`):** `fixture_id`, equipos, liga, estado final y marcador coinciden con el JSON suministrado.
2. **Procedencia verificada del proveedor (`_has_verified_provider_provenance`):** exige sobre estructura API-Football (`get == "fixtures"`, `parameters`) origen verificado (`_provenance` con `provider == "API-Football"` y `captured_at_utc`, o ruta sancionada en `outputs/matches/` / `outputs/api_cache/`, o `provider_provenance_verified=True`). Un JSON arbitrario con campos correctos pero sin procedencia acreditada queda como `UNVERIFIED_SOURCE_PROVENANCE` (`creditable = False`).
3. **Modo simulación (`--simulate`):** etiquetado `SIMULATED_TEST_ONLY` (`creditable = False`), aislado para pruebas.

## 3. Integridad de archivos y cuotas Bwin
- **Creación exclusiva atómica:** tanto el registro `pre_kickoff` como la liquidación separada (`settlements/<stem>.settlement.json`) se escriben con `open(..., "xb")`, impidiendo sobrescritura concurrente.
- **Verificación de `code_hashes`:** `load_frozen_configs` verifica los SHA-256 del código activo en `audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json`.
- **Cuotas Bwin (`extract_bwin_capture_utc`, `_classify_odds_status`):**
  - `NO_ODDS`: sin cuotas capturadas.
  - `ODDS_VERIFIED`: cuotas con `odds_sha256` y cabecera `Captura:` / `Fecha captura:` con zona horaria válida (`<= now_utc` y `< kickoff_utc`).
  - `ODDS_UNVERIFIED`: cuotas sin timestamp válido o fuera de ventana PREMATCH.
  - `evaluate_prospective_records` evalúa precisión deportiva (`RPS`, `Log-Loss`, `Brier`) sobre todos los registros acreditables (`settled_paired_n`), pero restringe la elegibilidad económica (`economic_eval_eligible_n`) exclusivamente a `ODDS_VERIFIED`.
