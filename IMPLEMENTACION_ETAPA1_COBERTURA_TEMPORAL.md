# Etapa 1 — seguridad temporal, cobertura y selección estadística

Implementación local terminada. No se consumió cuota API ni se ejecutó adquisición real, discovery, scraper o finish. Los snapshots originales permanecen intactos. Los cambios previos del árbol de trabajo se conservaron.

## Cambios implementados

- Política común en prematch_cache_record: disponibilidad explícita del resultado cuando existe y fallback conservador kickoff + 3 h; adquisición fechada separadamente. Las tres horas son una política declarada, no una hora oficial de finalización.
- generate_context: elimina fallback a históricos sin finalizar, filtra disponibilidad antes del corte y comprueba de nuevo los detalles. APIFootballClient acepta asof; en replay solo usa caché previa, sin fallback a red. Se preservan versiones anteriores de caché antes de reemplazar el puntero.
- Phase1: el corte fijo excluye respuestas posteriores incluso si preceden al kickoff. En modo live explícito, el corte avanza hasta la hora real de captura y no permite cruzar kickoff. Respuestas excluidas quedan registradas; no se borran sus evidencias. El filtro histórico usa min(corte,kickoff).
- preselect_matches: asof explícito es cache-only; adquisición live fecha el análisis al terminar y bloquea fixtures cuyo inicio se haya cruzado. Lleva fecha de adquisición hasta las observaciones. Rescore, descubrimiento de patrones, motor táctico y predictor rechazan procedencia conocida posterior al corte declarado.
- Orquestación: conserva referencia inicial de selección y corte efectivo de adquisición como conceptos distintos. Los lotes live no se fuerzan a una fecha previa a su captura. El consolidado conserva todos los ledgers; una reanudación rechaza lotes con adquisición acreditada posterior a generated_at. No se reescribieron evaluaciones existentes.
- leagues se consulta una vez por liga/temporada antes del plan de detalle, usando los clientes y cachés existentes. Se exige coincidencia de identidad/temporada y se conservan TRUE/FALSE/UNKNOWN y procedencia. Errores de consulta no se interpretan como FALSE.
- FALSE en estadísticas colectivas evita nuevos lotes de detalle de esa candidatura en preselección; FALSE en standings evita esa consulta. Se conserva la candidatura y su evidencia básica. coverage_probe_reason permite una comprobación explícitamente justificada. En Phase1 se planifica cada endpoint de estadísticas, eventos, XI y jugadores según su propia capacidad; UNKNOWN permite comprobación normal.
- ADVANCED exige statistics_fixtures=TRUE, standings=TRUE o excepción documentada y muestra suficiente observada en al menos una familia de detalle (córners, amarillas o remates). Tener goles históricos con statistics vacío no basta. Copas identificadas por el proveedor admiten excepción sin tabla; playoffs requieren coverage_exception_reason.
- Cada familia informa su n propio/concedido de ambos equipos, sin sumar torneos ni duplicados. Goles, córners, tarjetas (métrica amarillas), tiros y exposición individual quedan separados. Flags individuales no acreditan minutos ni validan props.
- Prioridad actualizada conectada a ranking estadístico, planificación adaptativa y cola táctica/operativa. Los flags adicionales solo otorgan bonificación de prioridad, condicionada a evidencia colectiva suficiente. No modifican distribuciones ni representan probabilidad.

## Archivos de implementación modificados en esta etapa

| Archivo | Responsabilidad |
|---|---|
| prematch_cache_record.py | Política temporal común y preservación de versiones |
| generate_context.py | Contexto/caché con corte e históricos finalizados |
| prematch_phase1.py | Corte de adquisición y planificación por cobertura |
| preselect_matches.py | Contratos, prioridades, familias, reglas, trazabilidad y replay |
| rank_matches.py | Planificación y conservación de campos de cobertura |
| prematch_daily.py | Bonificación de investigación y argumento de reglas |
| prematch_prospective_trial.py | Corte real de adquisición, reanudación, consolidación y reglas |
| prematch_tactical_history.py | Propagar disponibilidad/adquisición a observaciones |
| prematch_pattern_discovery.py | Aplicar corte y disponibilidad al consumo |
| prematch_tactical_signal_engine.py | Corte, validación de fuente y prioridad trazable |
| prematch_predictive_engine.py | Reutilizar política temporal y validar fuente; sin nuevas variables |
| config/coverage_priority.json | Reglas explícitas de referencia |

Pruebas: nuevo test/test_prematch_coverage_stage1.py y ajustes de expectativas en test/test_prematch_data_acquisition.py y test/test_prematch_phase1.py. Una prueba antigua situaba el histórico de noviembre después de su corte de enero: se corrigió su fecha de corte para que siga probando adquisición válida; el caso de resultado no disponible se prueba separadamente.

## Reglas configurables

Defaults: advanced_bonus=5, additional_capability_weight=0.5, minimum_sample=4, skip_declared_false_details=true. Se validan tipos/rangos. enabled=false conserva la puntuación anterior; el control temporal sigue activo. Para comparar también el coste de adquisición, skip_declared_false_details=false restaura la comprobación aun con FALSE.

Los tres comandos admiten --coverage-rules /ruta/absoluta/reglas.json: preselect_matches.py, prematch_daily.py y prematch_prospective_trial.py. El hash de reglas forma parte del contrato de evaluación y del checkpoint, evitando reusar lotes bajo reglas distintas. El archivo de referencia es config/coverage_priority.json; no se alteran las reglas de apuestas ni umbrales financieros.

## Comparación de ranking, sin API

Sobre el mismo snapshot archivado, solo se aplicó una capa comparativa del ranking; no es una nueva evaluación PREMATCH ni una promoción del snapshot. No había contratos leagues verificables previos al corte en la caché pertinente: los diez quedan UNKNOWN/SECONDARY. No se inventaron booleans para aparentar una mejora. No cambia ningún puesto.

| Fixture | Encuentro | Antes | Después | Puntuación antes/después |
|---|---|---:|---:|---|
| 1649062 | Defensor Sporting–Penarol | 1 | 1 | 60 / 60 |
| 1600663 | Platense–Atletico Balboa | 2 | 2 | None / None |
| 1600664 | Inca Aruba–Águila | 3 | 3 | None / None |
| 1648404 | Ferro Carril Oeste–Gimnasia Y Tiro | 4 | 4 | None / None |
| 1649469 | Monterrey W–Pachuca W | 5 | 5 | None / None |
| 1557213 | Sporting JAX W–DC Power W | 6 | 6 | None / None |
| 1645272 | FC Inter Wanica–Notch | 7 | 7 | None / None |
| 1559705 | Deportivo San Pedro–Marquense | 8 | 8 | None / None |
| 1559707 | Suchitepéquez–Antigua GFC | 9 | 9 | None / None |
| 1650155 | Oriental–Colón | 10 | 10 | None / None |

Prueba controlada exclusivamente sintética: candidato A con contrato completo y métricas válidas pasa de 50 a 57 (puesto 2→1); B con cobertura UNKNOWN conserva 54 (1→2). enabled=false recupera el orden original. TRUE con detalle vacío permanece SECONDARY, aunque haya goles históricos. Esta comparación demuestra aplicación de reglas, no mejora predictiva.

Comparación JSON: `outputs/discovery/2026-11-15_1500_0300/evaluations/20261115T195445143219Z_3987d11f552b/api_coverage_stage1/ranking_comparison.json`. Observaciones y campos de la capa comparativa: `ranking_overlay.json`. Fuente original SHA: `6def1a9185d970d64b5ac42baadbb612a9f798ac71ed0e7ffea988f8f0f6359d`. Cero llamadas nuevas; cero hits de contrato válido al corte.

## Validación

249 pruebas ejecutadas: 248 aprobadas y 1 omitida. Incluyen cobertura completa/FALSE/UNKNOWN, copa sin tabla, excepción justificada, TRUE con detalle vacío, resultado tardío, caché posterior al corte sin fallback a red, adquisición live hasta el corte y bloqueo al kickoff, deduplicación, comparación activada/desactivada, versionado, reanudación y flujo PREMATCH conectado. Transporte requests.Session.request bloqueado durante la batería para impedir conexiones reales. git diff --check sin errores.

Suites ejecutadas: test_prematch_coverage_stage1, test_prematch_phase1, test_prematch_data_acquisition, test_prematch_predictive_engine, test_prematch_predictive_temporal_guard, test_prematch_research_profiles, test_prematch_tactical_signal_engine, test_prematch_daily, test_manual_prematch_flow, test_prematch_execution_control, test_generate_context_identity, test_prematch_connected_e2e, test_prematch_history_recovery_v2, test_prematch_objective7_1 y test_prematch_objective7_2.

La suite global no está acreditada: al ampliar inicialmente la búsqueda, test_prematch_temporal_identity_contract produjo seis errores por la ruta preexistente test/outputs/phase3/bwin_reuse_integrity/selection/selected_match.json ausente. No se crearon snapshots para hacerla pasar ni se alteró ese contrato externo. La batería final anterior excluye esa suite dependiente de archivos faltantes.

## Limitaciones y siguiente uso

- El plan Pro y la disponibilidad remota por competición siguen sin verificarse; no se autorizaron consultas reales. La nueva ruta consultará leagues en la próxima adquisición autorizada.
- Sin tiempo explícito de finalización se aplica la política conservadora de tres horas. Documentos antiguos sin ledger fechado permanecen sin garantía completa de adquisición; los que sí acreditan captura posterior al corte se rechazan. No se reemplazan por mtime.
- El snapshot antiguo puede servir como comparación archivada; no debe promocionarse sin revisar sus cortes. Lotes antiguos fechados antes de sus propias adquisiciones necesitan un nuevo artefacto derivado con corte acreditado, nunca reescritura del original.
- Pesos y umbral muestral de prioridad son heurísticos configurables; no están calibrados como probabilidades ni validan ventaja financiera.
- No se ajustaron descanso multicompetición, rotaciones, viajes ni nuevas variables predictivas. No se hicieron commits, picks o finish.

La siguiente adquisición real requiere autorización de API y una ventana PREMATCH válida. Los comandos con --asof quedan disponibles para replay con caché verificable; no adquieren datos nuevos para completar el pasado.
