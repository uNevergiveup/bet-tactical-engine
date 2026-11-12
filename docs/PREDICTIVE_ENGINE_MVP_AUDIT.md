# PREMATCH Predictive Engine V1.0 MVP — auditoría de implementación

## Referencia y aislamiento

- Referencia congelada: tag `selector-v2.1.3-stable-offline` (`d61c1a2`).
- Rama de desarrollo: `feature/prematch-predictive-engine-v1-mvp`.
- El motor nuevo escribe bajo `outputs/predictive/1.0-mvp/<run_id>/` y no modifica artefactos V2.1.3.
- La capa predictiva no abre archivos Bwin ni produce picks, stakes o EV.

## Funciones reutilizadas

- `discover_fixtures.py`: calendario Europe/Madrid y control PREMATCH.
- `filter_major_leagues.py`: filtro vigente de competición, categoría y género.
- `preselect_matches.py`: históricos, estadísticas detalladas, caché, deduplicación, límite de frecuencia y distinción entre cero/ausente/no solicitado.
- `prematch_pattern_discovery.py`: limpieza temporal, cobertura observada, patrones descriptivos y exploratorios.
- `prematch_tactical_signal_engine.py`: contratos existentes de evidencia y separación deportiva/comercial; no es sustituido.

## Funcionalidad ya existente antes del MVP

- Ventana configurable 15:00–03:00 y horizonte completo.
- Evaluación por lotes sin usar `max-matches` como recorte global en el comando diario.
- Caché por consulta y SHA de artefactos de pipeline.
- Históricos FT anteriores al corte, deduplicados por fixture.
- Matriz de cobertura observada y patrones descriptivos/exploratorios offline.
- Vínculo comercial posterior y separado, con identidad API/Bwin.

## Código nuevo o ampliado

- Distribuciones Poisson y binomial negativa sin dependencias nuevas.
- Contracción de muestras pequeñas hacia una referencia de liga/temporada.
- Separación de intervalo aproximado de parámetro, intervalo predictivo, cobertura y estabilidad.
- Walk-forward expansivo con Brier, log-loss, MAE y log-score de distribución.
- Ranking por `fixture_id + sujeto + familia + periodo`, rankings por familia y global diversificado.
- Estados `RULE_BASED`, `EXPLORATORY_UNVALIDATED`, `TEMPORALLY_VALIDATED`, `INSUFFICIENT_EVIDENCE` y `CONTRADICTORY`.
- Ledger de API por endpoint/parámetros/caché/SHA para nuevas preselecciones y presupuesto con reserva.
- Salidas deterministas y manifest con hashes.

## Limitaciones expresas del MVP

- Primera y segunda parte quedan `INSUFFICIENT_EVIDENCE` si el snapshot no trae splits verificables.
- La comparabilidad de rivales solo está parcialmente representada por liga y temporada.
- Cambios de entrenador se señalan cuando la fuente los declara; todavía no hay segmentación automática por régimen.
- Árbitro y estado del marcador requieren evidencia histórica/temporal que la preselección actual no conserva de forma general.
- La binomial negativa se activa únicamente con 12 valores, sobredispersión y parámetro de dispersión estable.
- `TEMPORALLY_VALIDATED` requiere mejora fuera de muestra frente al baseline. Un ajuste correcto no basta.
- Las puntuaciones del ranking son heurísticas experimentales, no porcentajes ni probabilidades calibradas.

## Comandos

Reproducción offline determinista:

```bash
.venv/bin/python prematch_predictive_engine.py \
  --preselection outputs/discovery/2026-11-03_1500_0300/prospective_trial/enriched/preselection.json
```

Preparación real limitada de tres fixtures, con máximo de 28 consultas y reserva de 5 (23 utilizables):

```bash
.venv/bin/python discover_fixtures.py --date 2026-11-04 --start 15:00 --end 03:00
.venv/bin/python filter_major_leagues.py --input outputs/discovery/2026-11-04_1500_0300/fixtures.json
.venv/bin/python preselect_matches.py \
  --input outputs/discovery/2026-11-04_1500_0300/fixtures_major.json \
  --output-dir outputs/predictive/real-limited-source \
  --max-matches 3 --history-limit 20 --detail-limit 8 \
  --request-budget 28 --request-reserve 5
.venv/bin/python prematch_predictive_engine.py \
  --preselection outputs/predictive/real-limited-source/preselection.json
```

La prueba real limitada es una comprobación del flujo y no sustituye la evaluación completa del calendario.
