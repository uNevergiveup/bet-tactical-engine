# Cierre de criterios de aceptación — identidad Bwin

| Criterio | Estado | Evidencia |
|---|---|---|
| AC-01 catálogo reutilizable | CUMPLIDO OFFLINE | catálogo compartido deduplicado, metadatos de alcance y SHA; `prematch_prospective_trial.py` |
| AC-02 una captura, matching local | CUMPLIDO | prueba de 100 fixtures; una invocación compartida; reintento completo por fixture eliminado |
| AC-03 matching multiseñal | CUMPLIDO | equipos, aliases, orden, competición, categoría y tolerancia temporal en `bwin_identity_audit.py` |
| AC-04 diez features | CUMPLIDO | `test_bwin_identity_matching.py`; 10/10 |
| AC-05 casos reales | CUMPLIDO | siete casos con evidencia y SHA; `bwin_identity_regression.py` |
| AC-06 cero falsos aceptados | CUMPLIDO EN REFERENCIA | 0 falsos positivos en positivos acreditados y controles negativos; no extrapolado |
| AC-07 estado para todo fixture | CUMPLIDO | 154/154 filas en JSON, CSV y Markdown |
| AC-08 ambigüedad explicada | CUMPLIDO | candidatos, señales y `reason_codes`; Event ID nulo si no está verificado |
| AC-09 rendimiento reproducible | CUMPLIDO OFFLINE | 94→1 adquisiciones estructurales; benchmark congelado; duración browser nueva pendiente |
| AC-10 propagación | CUMPLIDO | preselección conserva Fixture ID; ranking/handoff conservan IDs separados; auditor con SHA llega a `case.json` |
| AC-11 comando de auditoría | CUMPLIDO | `bwin_identity_audit.py` sin red, con salida JSON/CSV/Markdown |
| AC-12 piloto online documentado | CUMPLIDO COMO PLAN | procedimiento limitado documentado; no ejecutado sin autorización |

## Resultado por feature

| Feature | Resultado reproducible |
|---|---|
| 1. Normalización | 30 pares positivos/negativos; aliases solo recuperan candidatos |
| 2. Horarios | UTC, CET, CEST, hora ausente y reprogramación preservados |
| 3. Categorías | 0 enlaces aceptados en los controles masculino/femenino/juvenil incompatibles |
| 4. Competiciones | homónimo Barcelona/Barcelona SC rechazado por competición |
| 5. Recuperación | objetivo real aparece con distractores y duplicado temporal |
| 6. Resolución | 7/7, precisión 1,0, recall 1,0, FP 0, FN 0 sobre verdad acreditada |
| 7. Catálogo compartido | 100 fixtures, un catálogo, una navegación declarada, sin red durante matching |
| 8. Búsqueda dirigida | evento ausente en general y recuperado en complemento con procedencia conservada |
| 9. Idempotencia | mismo snapshot produce las mismas filas sin red |
| 10. Integración | Fixture ID y Event ID permanecen distintos hasta handoff y expediente canónico |

El objetivo está cerrado en su parte offline. La validación online del tiempo real de captura y de cinco futuros queda deliberadamente pendiente de autorización; no constituye un bloqueo del contrato implementado, pero sí una limitación operativa que debe medirse antes de afirmar rendimiento online.
