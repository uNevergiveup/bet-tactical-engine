# Integración multimercado offline: alcance verificado

Estado: PYTHON_RESEARCH_INTEGRATED_COMMERCIAL_MODELS_PARTIAL, READY=false.
No adquisición ni servicios externos. Se reutilizó el recorrido estadístico
existente, sin recalcular las 14.517 observaciones ni reformular con holdout.

## Corrección e integración

El controlador conecta ahora las asociaciones avanzadas a cada fixture del
ranking y al dossier explicativo. Conserva condición, resultado, periodo,
entidad, muestras de entrenamiento/prueba, incertidumbre, contraejemplos,
aplicabilidad al rival y dependencia del XI. El ranking base conserva su orden:
el número de asociaciones exploratorias no mejora la prioridad ni sustituye
evidencia corroborada. Esto es integración de evidencia, no un nuevo modelo
de puntuación validado.

Se integraron 15.063 vínculos asociación–fixture de las 15.234 asociaciones de
la pasada avanzada existente; fueras de juego siguen excluidos comercialmente.
Una asociación puede vincular varios objetivos. No son 15.063 hallazgos
independientes. Las explicaciones Markdown muestran una por familia; el JSON
conserva todas, no sólo las seleccionadas para lectura.

Artefacto real: `outputs/control/runs/python_engine_real_v1/final_integration/`:
`integrated_research.json` y `explanations.md`. El JSON incluye hashes de las
cinco entradas, ranking completo, evidencia, explicaciones, auditoría de los
73 fixtures y causas del mapeo por fixture.

## Auditoría exacta de 1.477 patrones del adaptador existente

| Resultado | Número | Interpretación |
|---|---:|---|
| NO_VERIFIED_PREFLIGHT | 1401 | Sin captura verificada para el fixture; no prueba mercado inexistente |
| NO_EXACT_TEAM_MARKET_LINE_IN_VERIFIED_CAPTURE | 59 | Sin contrato exacto equipo/periodo aceptado por el adaptador conservador |
| NO_COMPATIBLE_TEAM_MODEL_IN_V1 | 16 | Métrica sin modelo comercial compatible implementado |
| DESCRIPTIVE_PATTERN_LACKS_MINIMUM_SAMPLE | 1 | Mercado enlazado, muestra insuficiente |

El último es goles del visitante Arsenal W, FT, fixture 1638295.
No existe un fallo global de identidad: cero bloqueos de identidad.
Los 1.477 son patrones del predictor descriptivo, distintos de los miles de
asociaciones avanzadas. No convertir sus umbrales medianos de entrenamiento
en líneas Bwin: sería una incompatibilidad estadística real.

Los 59 casos son ausencia de compatibilidad en el adaptador actual, no prueba
de que la familia no esté ofrecida. El adaptador exige mercado directo de
equipo, periodo explícito y propietario verificado; totales del partido y
props de jugadores requieren modelos distintos. La integración marca las
asociaciones exploratorias como pendientes de contrato/modelo y conserva
los vínculos reales del mapeador, sin inventar precios ni aceptar relaciones
del mismo partido como variables conocidas antes del kickoff.

## Auditoría del inventario y capturas

| Estado local | Fixtures |
|---|---:|
| Captura local auditada | 4 |
| No encontrado en páginas recorridas | 40 |
| Requiere revisión | 6 |
| Horario pendiente | 4 |
| Fuera del catálogo legado de 54 | 19 |

Total 73. Los 19 no fueron excluidos del inventario estadístico actual. El
catálogo previo conservaba el filtro antiguo de categorías; no se regeneró
porque requiere scraping prohibido en esta fase. Los cuatro verificados son
1638295, 1600658, 1600659 y 1559703. El primer intento Barcelona falló y se
preservó junto con la captura posterior correcta. No hay evidencia para
atribuir las otras ausencias a un límite de cuatro capturas ni para afirmar
que Bwin no ofrece esos eventos.

## Implementación, restricciones y pendientes

Implementado: integración por fixture/familia, contraevidencia, incertidumbre,
fuentes, explicaciones legibles, todos los fixtures, cruce conservador con
snapshots y auditoría de causas. Los tests verifican conservación de fixtures,
contraejemplos, incertidumbre y ausencia de autorización predictiva.

Restricciones de datos: sólo cuatro capturas, XI/roles objetivos pendientes,
cobertura desigual de jugadores y mitades. No se solucionan relajando gates.

Pendientes funcionales: modelos comerciales de totales y props individuales,
adaptación validada de asociaciones condicionales a líneas de mercado,
reconstrucción minuto a minuto de estados, calibración y validación predictiva.
El ranking integra evidencia avanzada para revisión, pero no reordena por
una puntuación avanzada aún no validada. No se declara motor comercial
completo ni READY. El protocolo futuro Codex Analyst usa razonamiento de
Codex; no exige contratar un runtime externo.

## Comando manual de integración sin recalcular estadísticas

```sh
.venv/bin/python -B tools/integrate_frozen_research.py \
 --run outputs/control/runs/python_engine_real_v1 \
 --advanced outputs/control/runs/python_engine_real_v1/advanced_score_states/advanced_patterns.json \
 --mapping outputs/control/runs/python_engine_real_v1/real_market_mapping/market_mapping.json \
 --discovery outputs/phase1/2026-11-12_1500_0300/browser_retry_02/bwin_scan/bwin_discovery.json \
 --output-dir outputs/control/runs/python_engine_real_v1/integration_manual_nueva
```

La carpeta destino debe ser nueva. Para jornadas nuevas usar prepare/dry-run/run
del manual existente, pasando cada preflight local mediante `--preflight`.
La integración automática vive en la etapa explanations del controlador.

Verificación: suite completa `tools/run_offline_tests.py` terminada con código
0, sin fallos, con protección de outputs originales y red bloqueada. También
se verificaron por separado las dos pruebas de integración avanzada y
`git diff --check`. El recorrido real de integración produjo 73 expedientes.
