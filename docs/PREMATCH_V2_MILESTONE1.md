# BET ENGINE V2 · Milestone 1

## Resultado

El flujo existente queda integrado hasta un expediente canónico manual por Event ID. No se creó un segundo motor: `prematch_case.py` es una fachada que adopta el resultado diario y delega adquisición, refrescos, validación y motor en `prematch_single.py`/`prematch_multi.py`.

## Diagnóstico resuelto

El handoff diario aislado era verificable, pero `prematch_multi retry-context` solo podía leer `outputs/matches/<EVENT_ID>/manifest.json`. Por ello un candidato elegido no podía continuar sin repetir preparación o construir manualmente estado. La adopción ahora:

1. valida el handoff con el puente existente;
2. fija selección, decisión, snapshot Bwin y sus hashes;
3. crea el mismo `manifest.json` que consumen los componentes existentes;
4. deja estado `ERROR_CONTEXTO` y siguiente acción `retry-context`, sin repetir Bwin;
5. conserva `case.json` como contrato inmutable de identidad, mientras `manifest.json` sigue siendo la única fuente operativa.

## Capacidades demostradas

- Reanudación idempotente y conflicto write-once.
- Identidad Event/Fixture/equipos/competición/kickoff fijada.
- Snapshot temprano conservado y refresco final delegado al componente que archiva la versión anterior.
- Contexto inicial y actualización diferencial de XI separados.
- Importación manual de hipótesis sin ejecutar LLM.
- Contraste Python mediante paquete de entrenamiento y holdout sellado; registro de aceptadas, exploratorias, rechazadas y muestra insuficiente.
- Importación cualitativa identificada y no simulada.
- Dossier JSON/Markdown que vuelve a verificar todos los hashes.
- Sin picks automáticos, cambios de umbral ni reglas comerciales.

## Regresión congelada

Pasto–Junior conserva exactamente Event ID `7912964` y Fixture ID `1549868` en la reconciliación y en `match_identity_7912964.json`. El modo histórico puede inspeccionarse desde la fachada, pero continúa siendo rechazado por el cargador accionable general.

## Verificación

- Regresiones focalizadas Objetivos 7/7.1/7.2 y V2: 42 pruebas correctas.
- Suite offline completa: 729 pruebas correctas, 10 omitidas por snapshots opcionales ausentes.
- Sin API-Football, Bwin, navegación web ni LLM externo durante la implementación y pruebas.

## Límites honestos

- No se realizó un piloto online nuevo; la autorización y los datos futuros no se simulan.
- Una hipótesis necesita un paquete estadístico compatible para poder contrastarse. Sin él queda `PENDING_PYTHON_VALIDATION`.
- Un snapshot de XI debe adquirirse mediante el mecanismo controlado existente antes de importarlo con `refresh-context`.
- La disponibilidad de mercado no equivale a modelo calibrado ni valor financiero.

## Estado

`MILESTONE_1_IMPLEMENTED_OFFLINE_VERIFIED`. Preparado para un piloto manual controlado; no declarado como apuesta lista ni como validación comercial general.

