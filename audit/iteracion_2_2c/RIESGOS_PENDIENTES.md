# Riesgos Pendientes (Iteración 2.2C)

1. **Ausencia de sellado temporal criptográfico externo (TSA):**
   - `pre_kickoff_sha256` y `record_file_sha256` verifican integridad local frente a alteraciones accidentales o discrepancias entre registro y liquidación, pero **no sustituyen** a una autoridad de sellado temporal (RFC 3161) ni a una firma asimétrica externa. Para no repudio fuerte, el `pre_kickoff_sha256` debe copiarse o enviarse a un registro externo antes del kickoff.
2. **Disponibilidad temporal Elo inferida cuando faltan timestamps de publicación:**
   - Los offsets `FT +125m`, `AET +165m`, `PEN +180m` y `UNKNOWN +180m` son estimaciones conservadoras. Si una competición o proveedor publica resultados con mayor retraso y no existe `observed_available_at_utc`/`finished_at_utc`/`published_at_utc`, no se puede garantizar ausencia absoluta de fuga temporal.
3. **Cero partidos futuros elegibles en `outputs/matches/` en el momento del cierre:**
   - A fecha `2026-11-18T10:50:49Z`, los 159 directorios existentes correspondían a eventos con kickoff pasado. La primera ejecución real sobre un partido futuro queda pendiente de la próxima captura operativa PREMATCH.
4. **Muestra prospectiva real insuficiente (`n = 0`):**
   - Los umbrales de acreditación (`CLUB >= 200`, `NATIONAL_TEAM >= 50`) siguen en `0`. No debe activarse `calibration_accredited_families` ni promocionarse ningún modelo candidato.
5. **Fallos/errores preexistentes en suites legadas del repositorio:**
   - La suite completa del repositorio mantiene fallos/errores previos a la etapa 2.2 fuera de los módulos del motor actual (comparación exacta de identificadores en `TESTS_ITERACION_2_2C.log`).
