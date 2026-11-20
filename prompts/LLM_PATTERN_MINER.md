# LLM Pattern Miner · contrato manual

Este prompt se usa manualmente después de que Python haya producido el paquete estadístico. No autoriza navegación, adquisición ni ejecución automática de otro modelo.

## Entrada obligatoria

- Un solo Event ID y Fixture ID.
- `cutoff_utc` anterior al kickoff.
- Observaciones de entrenamiento locales; no abrir el holdout sellado.
- Patrones Python, cobertura por familia, contradicciones y referencias SHA-256.

## Tarea

Propón únicamente hipótesis nuevas, falsables y comprobables por Python. Busca interacciones entre producción propia y concesión rival, localía, periodo, formación, entrenador, jugadores, exposición y fortaleza comparable. Distingue correlación de mecanismo plausible, registra contraejemplos y no declares validación, probabilidad, valor ni causalidad.

Devuelve JSON con este contrato:

```json
{
  "schema_version": "1.0-manual-pattern-hypotheses",
  "event_id": "<EVENT_ID>",
  "fixture_id": 123,
  "hypotheses": [
    {
      "id": "h-001",
      "variables": ["historical_blocked_shots", "historical_corners"],
      "evidence": ["fixture:123", "sha256:..."],
      "conditions": {"locality": "home", "period": "FT"},
      "mechanism": "Explicación que no se confunde con prueba causal",
      "refutation": "Resultado observable que rechazaría la hipótesis",
      "prematch_availability": "HISTORICAL_AGGREGATE",
      "limitations": ["muestra pequeña"],
      "python_test": {
        "segment": {"entity_type": "team", "team_id": 1, "player_id": null, "league_id": 2, "season": 2026, "gender": "male", "team_kind": "senior", "period": "FT"},
        "condition": {"field": "historical_blocked_shots", "operator": "GT", "value": 3.5},
        "outcome": "corners",
        "threshold": 4.5,
        "evidence_fixture_ids": [101, 102]
      }
    }
  ]
}
```

`prematch_availability` solo admite `OBSERVABLE`, `HISTORICAL_AGGREGATE`, `XI_DEPENDENT` o `POSTMATCH_ONLY`. Las variables `POSTMATCH_ONLY` se registran pero no pueden alimentar una predicción PREMATCH. Python decide después `ACEPTADA`, `RECHAZADA`, `EXPLORATORIA` o `MUESTRA_INSUFICIENTE` mediante separación temporal y controles de multiplicidad.

