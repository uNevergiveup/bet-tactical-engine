# Auditoría del Registro Prospectivo PREMATCH (Iteración 2.2C)

## 1. Alcance y estado de componentes intocados
- `CoherentMatchModeler` (producción): **intacto**.
- `calibration_accredited_families`: **desactivado (`frozenset()`)**.
- Controles P0 y expedientes históricos (`outputs/matches/`): **intactos**.
- Modelos congelados en `audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json`: `M0`, `M0_plus_Elo`, `M4`, `M4_plus_Elo` (parámetros y `train_fit_base_rates` inalterados; únicamente se actualizaron los `code_hashes` tras los endurecimientos 2.2C).

## 2. Garantías de persistencia e integridad verificadas
1. **Control temporal estricto en ejecución (`record`):**
   - Exige `now_utc < kickoff_utc`, `cutoff_utc <= now_utc`, `cutoff_utc < kickoff_utc`, estado distinto de `LIVE_OR_FINISHED_STATUSES` y ausencia de goles previos (`goals.home is None` y `goals.away is None`).
   - El corte del corpus (`CORPUS_CUTOFF_UTC = 2026-11-14T19:45:00+00:00`) **no sustituye** la comprobación de hora real: si `kickoff_utc > CORPUS_CUTOFF_UTC` pero `now_utc >= kickoff_utc`, el registro se rechaza con `RETROSPECTIVE_RECORD_FORBIDDEN`.
2. **Prohibición de sobrescritura silenciosa:**
   - Si `<fixture_id>_<event_id>.json` ya existe, `record_prospective_prediction` lanza `IMMUTABLE_RECORD_ALREADY_EXISTS`. Incluso pasar `allow_overwrite=True` lanza `OVERWRITE_PROSPECTIVE_RECORD_FORBIDDEN`.
3. **Mismo estado informativo para los cuatro modelos:**
   - `ObservationStore.from_match_engine_json` se ejecuta una sola vez con `cutoff_utc`. Los 4 modelos reciben copias independientes de la misma lista de partidos observados y el mismo `elo_tracker`.
   - Cada predicción incluye `info_state_sha256`, idéntico al `observations_sha256` del bloque `provenance`.
4. **Cobertura de hashes (`pre_kickoff_sha256`):**
   - `pre_kickoff_sha256` cubre el bloque completo `pre_kickoff`, incluyendo:
     - **Datos:** `input_sha256`, `match_engine_payload_sha256`, `observations_sha256`, `odds_sha256`.
     - **Versiones:** `registry_schema_version` (`2.2C-prospective-record`), `frozen_manifest_version`.
     - **Parámetros:** `manifest_sha256`, `model_params_sha256` (hash canónico de `ExperimentalConfig` de los 4 modelos).
     - **Procedencia:** `elo_tracker_provenance`, `source_paths`.
5. **Inmutabilidad real del archivo original tras `settle`:**
   - En 2.2B, `settle_prospective_record` escribía `settlement` dentro del mismo JSON original.
   - En **2.2C**, `settle_prospective_record` **no modifica el archivo original**; escribe en un archivo separado `settlements/<fixture_id>_<event_id>.settlement.json` con `record_file_sha256` y `pre_kickoff_sha256`.
   - Cualquier alteración posterior del archivo original invalida `pre_kickoff_sha256` y `record_file_sha256`, provocando rechazo inmediato en `settle` y `evaluate`.

## 3. Auditoría temporal Elo
- `FT (+125m)`, `AET (+165m)`, `PEN (+180m)` y `UNKNOWN (+180m)` se etiquetan explícitamente como **estimaciones conservadoras** (`ESTIMATED_CONSERVATIVE_*`), no como timestamps verificados.
- `resolve_elo_available_dt` prioriza timestamps verificables (`observed_available_at_utc`, `finished_at_utc`, `published_at_utc` -> `VERIFIED_TIMESTAMP`).
- Pruebas añadidas en `test_iteracion_2_2c_registry_and_elo.py` cubren: partidos simultáneos, finalización/publicación tardía después del offset estimado, prórroga (`AET`), e interrupciones/suspendidos (`INT`, `SUSP`).
- **Limitación explícita:** Cuando la disponibilidad es inferida mediante offset estimado y falta un timestamp de publicación verificado, **no es posible garantizar ausencia absoluta de fuga temporal** si un proveedor publicó el resultado más tarde del offset estimado.

## 4. Distinción entre integridad local y garantías criptográficas
- **Lo que el sistema SÍ proporciona:** Integridad local verificable mediante `SHA-256` canónico (`pre_kickoff_sha256` y `record_file_sha256`) para detectar alteraciones accidentales o modificaciones posteriores no consistentes entre el registro original y su liquidación separada.
- **Lo que el sistema NO proporciona:** Garantías criptográficas de no repudio ni sellado temporal confiable (RFC 3161 TSA, firma asimétrica con clave privada externa o anclaje en registro externo inmutable). Un operador con acceso de escritura al sistema de archivos y al reloj local podría regenerar ambos hashes o alterar la hora del sistema si no se archiva externamente el `pre_kickoff_sha256`.
