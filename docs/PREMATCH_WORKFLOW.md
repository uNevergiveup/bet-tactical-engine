# PREMATCH V2 · flujo integrado

## Fuente canónica

Cada partido vive en `outputs/matches/<EVENT_ID>/`. `manifest.json` es la única fuente operativa; `case.json` fija identidad, Fixture ID, equipos, competición, kickoff, selección y handoff de origen por SHA-256. El flujo diario conserva sus evaluaciones write-once, pero deja de ser el estado operativo una vez adoptado el candidato.

## Fases

1. `prematch_daily.py`: descubrimiento amplio, ranking estadístico separado de preparación comercial y handoff validado.
2. `prematch_case.py prepare`: adopta el handoff sin redescubrir y crea el manifiesto del evento con el snapshot Bwin existente.
3. `acquire-context`: delega en el reintento API inicial del evento; las cachés y límites existentes siguen vigentes.
4. Python calcula cobertura y patrones. La elegibilidad es por familia; identidad verificada no implica cobertura suficiente.
5. `attach-hypotheses`: importa hipótesis manuales con identidad y contrato verificables. No las valida narrativamente.
6. `attach-research`: incorpora un informe cualitativo identificado, sin afirmar que el proveedor fue ejecutado por el pipeline.
7. `refresh-odds`: captura final del mismo Event ID y archiva el snapshot anterior. No redescubre la jornada.
8. `refresh-context` incorpora un snapshot local de XI/contexto adquirido por el mecanismo controlado; los artefactos posteriores quedan invalidados y deben recalcularse.
9. `finalize`: valida, ejecuta el motor existente y genera el prompt específico del evento.
10. `export-handoff`: produce dossier JSON/Markdown autocontenido con hashes y limitaciones para el analista Codex.

No hay picks automáticos. Una señal descriptiva, una hipótesis exploratoria, un modelo implementado y un modelo comercial calibrado son estados distintos.

