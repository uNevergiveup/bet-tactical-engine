# Runbook manual PREMATCH V2

Use `.venv/bin/python` si existe; en los ejemplos se representa como `$PY`.

```bash
PY=.venv/bin/python

# 1. Jornada y handoff (operación online explícita)
$PY prematch_daily.py --confirm-external --date hoy

# 2. Adoptar exactamente el handoff validado
$PY prematch_case.py prepare --handoff /ruta/al/handoff_prematch.json

# 3. Consultar sin mutar
$PY prematch_case.py status --event-id EVENT_ID

# 4. Completar contexto del mismo evento; no repite Bwin
$PY prematch_case.py acquire-context --selected /ruta/selected_match.json

# 5. Adjuntar hipótesis manuales y reporte cualitativo
$PY prematch_case.py attach-hypotheses --event-id EVENT_ID --file llm_pattern_hypotheses.json
$PY prematch_case.py validate-hypotheses --event-id EVENT_ID --packet training_packet.json --sealed sealed_holdout.json --ranking ranking.json
$PY prematch_case.py attach-research --event-id EVENT_ID --file reporte_cualitativo.md

# 6. Refrescar solo las cuotas del Event ID ya fijado
$PY prematch_case.py refresh-odds --selected /ruta/selected_match.json

# Incorporar el snapshot final de contexto/XI, fijado también por SHA
$PY prematch_case.py refresh-context --selected /ruta/selected_match.json -- --snapshot /ruta/lineups.json --snapshot-sha256 SHA256

# 7. Validar y motorizar; no ejecuta Codex
$PY prematch_case.py finalize --selected /ruta/selected_match.json -- --report /ruta/reporte_cualitativo.md

# 8. Exportar el dossier verificando todos los hashes
$PY prematch_case.py export-handoff --event-id EVENT_ID
```

Las operaciones externas exigen confirmación explícita y se bloquean tras kickoff o fuera del margen configurado. `status` y `export-handoff` son locales. Si falta DeepSeek/LLM, se omite `attach-hypotheses`: el expediente continúa con `NOT_SUPPLIED`, nunca con investigación simulada.

Para un snapshot histórico use `prepare --historical --asof <ISO8601>`; queda marcado como auditoría y nunca como apuesta accionable.

