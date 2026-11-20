# PREMATCH V2 · aceptación de Milestone 1

| Criterio | Evidencia | Estado |
|---|---|---|
| AC-01 identidad canónica | `case.json`, `manifest.json`, hashes e IDs comprobados | IMPLEMENTADO |
| AC-02 handoff diario reutilizable | adopción transaccional, estado `ERROR_CONTEXTO`, siguiente acción `retry-context` | IMPLEMENTADO |
| AC-03 write-once y reanudación | conflicto de contenido bloqueado; adopción idéntica idempotente | IMPLEMENTADO |
| AC-04 control temporal PREMATCH | ventanas existentes de `prematch_single`/`prematch_multi` | IMPLEMENTADO |
| AC-05 adquisición/caché | reutiliza generador, ledger y cachés actuales; no se duplica adquisición | IMPLEMENTADO EN COMPONENTES EXISTENTES |
| AC-06 hipótesis manuales | contrato, identidad, conservación inmutable y estado pendiente de Python | IMPLEMENTADO; VALIDACIÓN DEPENDE DE PAQUETE ESTADÍSTICO |
| AC-07 Bwin temprano/final | snapshot inicial por hash y refresco con archivo previo archivado | IMPLEMENTADO EN COMPONENTES EXISTENTES |
| AC-08 investigación cualitativa | importación identificada, write-once, no ejecución automática | IMPLEMENTADO |
| AC-09 dossier final | JSON/Markdown con cobertura, fuentes, hashes y limitaciones | IMPLEMENTADO |
| AC-10 ausencia de picks automáticos | todos los artefactos declaran `actionable_bet=false` | IMPLEMENTADO |

## Escenarios

- A. Candidato diario: adopción probada sin repetir descubrimiento.
- B. Pasto–Junior (Event 7912964 / Fixture 1549868): regresión congelada de identidad y modo histórico en `test_prematch_case.py`.
- C. Cobertura parcial: el dossier distingue contexto, hipótesis, cualitativo y Bwin.
- D. Sin XI: no se promociona a oficial; el refresco diferencial invalida salidas posteriores.
- E. Actualización antes del kickoff: permitida por las ventanas T-5 existentes.
- F. Después del kickoff: bloqueada para operación PREMATCH; solo auditoría histórica explícita.
- G. Fallo externo: permanece estado técnico/contexto pendiente y el snapshot original no se sobrescribe.
- H. Sin LLM/DeepSeek: `NOT_SUPPLIED`; no se simula ejecución.
- I. Identidad o hash discordante: bloqueo cerrado.

Pruebas principales: `python3 -m unittest -v test_prematch_case.py`. La suite completa debe ejecutarse offline antes de publicar el milestone.

