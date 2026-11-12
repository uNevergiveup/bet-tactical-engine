# Objetivo 6.1 — ampliación internacional controlada

Partida: commit `02efcb82`, baseline `outputs/control/runs/objective6_STATISTICAL_BASELINE_PREMATCH_v1/`. La ampliación conserva ese directorio íntegro. Los ocho expedientes nuevos están en `outputs/control/runs/objective6_1_international_final_v1/<Fixture ID>.json` y `.md`.

La ejecución utiliza exclusivamente estadísticas y procedencia locales congeladas. Se conserva el corte del baseline por fixture, incluso si el partido ya ha comenzado cuando se consulta el expediente. No se acredita vigencia operativa actual ni se usa información posterior al corte. No se ejecutó navegación, adquisición, investigación cualitativa, actualización de XI/cuotas ni picks.

## Cobertura comparada

El baseline tiene cuatro antecedentes por equipo en Nations League 2026. La ampliación inventaría antecedentes adicionales, sin convertirlos en partidos de la misma competición:

| Fixture | Partido | Históricos locales local / visitante | Señales principales EXPLORATORIO / FRAGIL / DESCARTADO |
|---|---|---:|---:|
| 1528958 | Türkiye–Belgium | 20 / 19 | 10 / 18 / 20 |
| 1528959 | Armenia–Cyprus | 16 / 17 | 0 / 48 / 0 |
| 1528960 | Italy–France | 16 / 20 | 13 / 17 / 18 |
| 1528961 | Czechia–Spain | 18 / 19 | 20 / 17 / 11 |
| 1528962 | England–Croatia | 19 / 20 | 11 / 18 / 19 |
| 1528963 | Albania–Finland | 17 / 16 | 0 / 48 / 0 |
| 1528964 | San Marino–Belarus | 17 / 17 | 0 / 48 / 0 |
| 1545613 | Montenegro–Latvia | 18 / 16 | 0 / 48 / 0 |

Los tamaños son fixtures disponibles, no garantía de todos sus campos. Cada JSON desglosa conteos válidos y ausentes por métrica, equipo, temporada, competición, localía y antigüedad (0–180, 181–365, más de 365 días), con IDs de evidencia y estadísticas descriptivas. Los 16–20 antecedentes proceden de la adquisición anterior limitada; no constituyen el histórico internacional completo de cada selección.

Las competiciones verificadas en payloads locales para la muestra principal son Nations League (5), clasificación europea al Mundial (32) y Mundial (1). Son antecedentes sénior, con contextos e incentivos diferentes: los modelos mantienen el estrato de competición-temporada y la auditoría lo explicita. Los amistosos (10) se añaden solo en análisis de sensibilidad separado. Baltic Cup (849) y FIFA Series (1222) permanecen inventariadas pero excluidas del ajuste principal por comparabilidad no establecida. Los payloads pueden contener metadatos de otros torneos: eso no los introduce en el análisis.

## Modelos y validación

Se analizan producción propia y concesión para goles, córners, amarillas, faltas, remates y tiros a puerta. Son familias candidatas para comprobación posterior de contratos reales Bwin; las amarillas no se equiparan automáticamente a puntos o totales comerciales de tarjetas. No se incorporan fueras de juego.

Dos modelos exploratorios, parámetros fijados antes de ejecutar:

- Media ponderada por antigüedad, con semivida de 365 días.
- Contracción jerárquica empírica de la media del estrato competición-temporada hacia la media ponderada de antecedentes de la misma selección. Peso previo fijo de cuatro observaciones; se registra cuánto pesa el estrato actual. No es un modelo bayesiano calibrado ni una estimación de fuerza relativa entre selecciones.

La media expansiva simple constituye el modelo base. Se evalúa cada partido histórico secuencialmente con al menos seis antecedentes válidos disponibles antes de su kickoff; el partido que se evalúa nunca entra en el ajuste. La comparación usa el mismo conjunto de partidos para candidato y baseline. Se conservan errores cuadrados individuales, medias de error por mitades temporales y contraejemplos donde el candidato empeora.

Se informa dispersión, varianza/media y tamaño efectivo de muestra de Kish. Los intervalos descriptivos remuestrean fixtures con semilla fija, sin tratar las perspectivas de los dos equipos como observaciones independientes. El intervalo de media es no ponderado y no debe interpretarse como intervalo predictivo del modelo ponderado. La dependencia serial no queda plenamente modelada: limita toda promoción predictiva. Los valores p exploratorios por cambios de signo se corrigen globalmente con BY; las pruebas comparten históricos, no representan corroboración independiente. No se eligieron parámetros mediante optimización en prueba.

La producción propia de cada equipo se acompaña de la concesión rival en la familia correspondiente, con muestra, pesos y dispersión separados. Su coincidencia es contexto estadístico: no se fusionan las dos medias en una probabilidad del partido.

## Categorías

- `INSUFFICIENT_SAMPLE`: menos de seis antecedentes válidos para la estimación.
- `FRAGIL`: menos de ocho evaluaciones secuenciales, tamaño efectivo menor de ocho o mejora que no conserva dirección entre mitades temporales. Se conserva solo como hipótesis para investigación cualitativa.
- `DESCARTADO`: con ocho evaluaciones o más, error medio igual o superior al baseline.
- `EXPLORATORIO`: mejora al baseline, ocho evaluaciones y tamaño efectivo suficientes, con mejora en ambas mitades. No acredita significación corregida ni generalización.
- `VALIDADO`: requiere evidencia independiente reservada, soporte y estabilidad temporal y controles de dependencia/multiplicidad. Ninguna señal de esta ejecución se promueve: los datos congelados ya se habían examinado.

## Resultado Python antes de Codex

768 evaluaciones de señales: 8 fixtures × 2 equipos × 6 familias × 2 direcciones × 2 modelos × 2 alcances. No son 768 patrones independientes.

| Alcance | EXPLORATORIO | FRAGIL | DESCARTADO | VALIDADO |
|---|---:|---:|---:|---:|
| Competitivo principal | 54 | 262 | 68 | 0 |
| Sensibilidad con amistosos | 70 | 175 | 139 | 0 |
| Total | 124 | 437 | 207 | 0 |

Se recupera capacidad exploratoria especialmente en córners de Bélgica, remates propios de Chequia y Croacia y remates concedidos por Francia e Italia. Son mejoras de error histórico, no pronósticos comerciales. La corrección múltiple impide elevarlas a evidencia validada. En las otras cuatro parejas toda la muestra principal queda frágil por escasez o inestabilidad. La sensibilidad con amistosos cambia muchos resultados, evidencia de dependencia del contexto competitivo.

El baseline original permanece `PARTIAL` para las ocho parejas. La ampliación añade hipótesis y modelos exploratorios sin declarar suficientes los cuatro antecedentes de la edición actual.

## Secuencia Codex → Python

Codex leyó los resultados generados por `objective6_1_python_v1` y registró dos alternativas falsables en `config/objective6_1_codex_hypotheses.json`: una ventana reciente de cinco encuentros para remates y el subconjunto de la misma localía para córners. Las referencias, explicaciones alternativas, refutación y limitación por selección posterior figuran en el registro.

La segunda ejecución Python las contrasta para las 16 selecciones, contra la media expansiva, y conserva todos los resultados en `codex_additional` de cada expediente. Ningún contraste de Codex es validación independiente: las hipótesis surgieron después de observar estos datos. Las preguntas cualitativas futuras están registradas, sin realizar investigación ahora.

Resultado de los 32 contrastes adicionales: 19 `INSUFFICIENT_SAMPLE`, ocho `FRAGIL` y cinco `DESCARTADO`. No se recupera una señal adicional exploratoria robusta con estas dos transformaciones.

## Reproducción y controles

```bash
.venv/bin/python -B prematch_international_expansion.py \
  --baseline outputs/control/runs/objective6_STATISTICAL_BASELINE_PREMATCH_v1 \
  --observations outputs/control/runs/python_engine_real_v1/advanced/historical_observations.json \
  --hypotheses config/objective6_1_codex_hypotheses.json \
  --output-dir outputs/control/runs/objective6_1_replay_NEW

.venv/bin/python -m unittest -q test_prematch_international_expansion.py \
  test_prematch_statistical_baseline.py test_prematch_advanced_discovery_v2.py \
  test_prematch_commercial_models.py
```

35 pruebas correctas. Incluyen exclusión temporal de objetivo/futuro y disponibilidades, ausencia distinta de cero, pesos por edad, tamaño efectivo, contracción por estrato y separación de fragilidad/descarte. La escritura exige directorio nuevo. Cada expediente referencia hash de su baseline; el manifiesto final sella inputs, procedencia, script y salidas.

Se repitió el flujo completo en `objective6_1_reproduction_v1`: los 17 hashes de artefactos y 222 hashes de procedencia coinciden exactamente. Los 44 artefactos sellados del baseline original siguen coincidiendo con su manifiesto. En el alcance competitivo principal el tamaño efectivo ponderado observado va de 8,51 a 15,30 según equipo/familia; este número no aumenta la muestra de evaluación ni certifica independencia temporal.
