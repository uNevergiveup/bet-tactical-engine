# Motor Python V1: verificación real offline del 12/11/2026

Estado: PARTIAL. Esta entrega no acredita el objetivo completo ni READY.

Se ejecutó el controlador completo con API=0, Bwin=0 y sin runtime externo,
reutilizando inventario y caché adquiridos. Resultado preservado en
`outputs/control/runs/python_engine_real_v1/`. No se alteraron originales.
El protocolo Analyst queda para la selección manual posterior; su ejecución
por Codex no requiere contratar otro LLM. El estado RLM_PENDING del controlador
es una dependencia heredada, no un bloqueo del cálculo estadístico Python.

## Evidencia observada

- Inventario y ranking: 73 fixtures, sin exclusiones silenciosas.
- Observaciones: 2.371 colectivas y 12.146 individuales.
- Primera pasada avanzada: 14.960 asociaciones descriptivas; ninguna corroborada.
- Segunda pasada independiente `advanced_score_states/`: 15.234 asociaciones,
  incorpora estado oficial al descanso y concesión de goles por mitad.
  No se reutiliza el holdout para reformular hipótesis: es una extensión de
  ingeniería, no una demostración confirmatoria ni evaluación predictiva.
- Ranking y explicaciones de la primera pasada: `base_ranking.json`,
  `explanations.json`, `explanations.md`.
- Cuatro TXT Bwin auditados offline en `local_preflight/`.
- Cruce real en `real_market_mapping/`: 1.477 patrones; 0 matched,
  1.401 pendientes de cuotas, 1 pendiente de validación,
  59 mercado no disponible y 16 no soportados. Cero bloqueos de identidad.
  Esos conteos clasifican patrones, no las 613 selecciones del inventario Bwin.

## Cobertura efectiva de la primera pasada

| Familia | FT | 1T | 2T | Estado |
|---|---:|---:|---:|---|
| Goles propios | 2371 | 2331 | 2331 | Calculado, descriptivo |
| Córners propios | 875 | 808 | 806 | Calculado con ausencias |
| Faltas propias | 860 | 250 | 250 | Parcial por cobertura |
| Amarillas propias | 891 | 774 | 774 | Calculado con ausencias |
| Remates propios | 858 | 808 | 806 | Calculado con ausencias |
| Remates a puerta | 873 | 808 | 806 | Calculado con ausencias |

Los conteos son filas con valor numérico, no fixtures independientes.
Hay formación en 11.594 observaciones, entrenador en 11.951 y fuerza rival
histórica en 162. No prueban formación, técnico o XI actuales del objetivo.
Los perfiles de jugadores y duelos están en `phase2/`; faltas provocadas versus
cometidas son compatibilidad estadística, no interacción directa acreditada.

## Límites funcionales que siguen abiertos

1. El ranking base consume el predictor descriptivo; no incorpora de manera
   completa las asociaciones avanzadas y sus contradicciones. La existencia
   de ambos artefactos no demuestra integración profunda multimercado.
2. El cruce real no ha producido matches aceptados. Hay que trazar las razones
   por contrato/familia y conectar patrones avanzados antes de cerrar el motor.
3. Estado al descanso implementado; reconstrucción minuto a minuto pendiente.
   No se reparten totales FT según minutos ni se infieren estados ausentes.
4. XI objetivo, árbitro acreditado, roles y duelos directos no se acreditan
   por perfiles históricos. Sus efectos probabilísticos no están validados.
5. Ninguna asociación está autorizada como modelo predictivo o valor financiero.
6. Las explicaciones actuales no cubren de forma integrada toda la evidencia
   avanzada; no equivalen al dossier final requerido.

## Reproducción manual

Desde la raíz, preparar siempre una carpeta y plan nuevos:

```sh
.venv/bin/python -B prematch_daily.py prepare --date 2026-11-12 --start 15:00 --end 03:00 --history 20 --api-budget 0 --bwin-events 0 --fixtures-input outputs/phase1/2026-11-12_1500_0300/fixtures_discovered.json --bwin-input outputs/phase1/2026-11-12_1500_0300/browser_retry_02/bwin_scan/bwin_discovery.json --plan outputs/control/plans/python_engine_manual_v2.json --output-dir outputs/control/runs/python_engine_manual_v2
.venv/bin/python -B prematch_daily.py dry-run --plan outputs/control/plans/python_engine_manual_v2.json
.venv/bin/python -B prematch_daily.py run --plan outputs/control/plans/python_engine_manual_v2.json
```

Para incluir capturas locales añadir `--preflight RUTA` repetible a prepare.
No confundir una captura histórica con precio vigente. La primera ejecución
real anterior se conserva y no debe reanudarse con código cambiado.

Verificación de los cambios: 26 pruebas focalizadas pasaron. Esta evidencia
acompaña el recorrido real y no sustituye los límites funcionales anteriores.
