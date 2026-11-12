# Objetivo 3: cruce y modelos comerciales locales

Estado: PARTIAL, READY=false. Sin API, scraping, LLM, picks ni stakes.
Se conservaron los cambios pendientes existentes; no se creó otra jornada.

## Antes y después sobre los mismos snapshots

| Estado de los patrones originales | Antes | Después |
|---|---:|---:|
| Sin captura verificada | 1401 | 1401 |
| Sin contrato exacto | 59 | 58 |
| Sin modelo compatible | 16 | 16 |
| Muestra insuficiente | 1 | 2 |
| Comercialmente evaluables con validación | 0 | 0 |

La mejora funcional no es una aceptación comercial: el contrato Barcelona
antes perdido por alias ahora se enlaza y llega correctamente al bloqueo de
muestra. Se reconocen además 120 selecciones literales de conteo con modelo
exploratorio, frente a ninguna evaluación de esa capa antes. No son 120 modelos
independientes ni 120 patrones originales aceptados. No hay valor demostrado.

## Auditoría individual

Directorio: `outputs/control/runs/python_engine_real_v1/commercial_v2/`.

- `cases59.json`: los 59 patrones originales, IDs, equipo, periodo, patrón
  completo, contratos observados con selección/línea/cuota, todas las cabeceras,
  contrato requerido, campo incompatible, clasificación y resolución.
- `cases16.json`: ocho offsides excluidos por regla del proyecto y ocho xG.
  xG es una expectativa, no un conteo liquidable de goles. No hacen falta
  16 modelos nuevos ni se deben eliminar esos bloqueos.
- `mapping_after/`: repetición real del mapeador sobre los cuatro preflights.
- `snapshots.json`: metadatos originales y SHA256 de las cuatro capturas.
- `models_validation.json`: 120 evaluaciones por selección literal, historial,
  parámetros, incertidumbre, sobredispersión y evaluación temporal.
- `conditional_features.json`: clasificación de todas las condiciones avanzadas.
- `integrated_research.json`: ranking deportivo conservado y evaluaciones
  incorporadas a ranking y expedientes explicativos.

Clasificación exacta de los 59: 38 familias ausentes en la captura, 20 con
totales de partido en lugar de totales de equipo, uno con alias defectuoso.
No se hallaron 59 errores homogéneos. La dirección del patrón descriptivo no
es una selección Over/Under: la dirección y la línea se toman exclusivamente
del contrato capturado y quedan registradas por selección.

## Defecto corregido

`FC Barcelona (F)` frente a `Barcelona W`: prefijo institucional FC y marcador
de categoría equivalente. Se añade comparación exacta de tokens conservando
género, únicamente dentro de un fixture cuya identidad ya se verificó.
No se permite masculino/femenino ni Barcelona/Arsenal. No se cambian mínimos,
precios, reglas ni liquidaciones. Los dos patrones ahora pendientes de muestra
son goles FT local y visitante del fixture 1638295.

## Matriz de modelos y contratos

| Familia/contrato | Implementación | Validación comercial |
|---|---|---|
| Conteo de equipo, línea x.5 | CDF empírica suavizada Jeffreys | No calibrado |
| Total conjunto, línea x.5 | Suma propia+concedida del mismo fixture histórico, sin independencia Poisson supuesta | No calibrado |
| Goles FT/1T/2T | Exclusivamente históricos del periodo literal | No calibrado |
| Córners FT/1T/2T | Mismo estimador cuando existe dato | No calibrado |
| Faltas/remates colectivos | Estimador disponible; contrato y cobertura necesarios | No acreditado en capturas |
| Tarjetas | Bloqueado hasta acreditar qué tarjetas/puntos liquida Bwin | No disponible |
| Props individuales | Bloqueado por identidad, XI y exposición objetivo | No disponible |
| Hándicap/resultado/combinados | Reconocidos como incompatibles con conteo simple | Modelo específico pendiente |
| Líneas enteras/cuartos | Bloqueadas: push y liquidación asiática requieren otro contrato | No disponible |
| xG/offsides | Sin equivalencia comercial autorizada | Excluidos |

El estimador empírico admite ceros y sobredispersión sin imponer Poisson. No
usa medias o medianas como líneas. Los totales conservan la dependencia
observada entre ambos contendientes en cada antecedente, deduplican fixture y
rechazan totales contradictorios. No es un modelo ajustado de interacción del
futuro emparejamiento; la mezcla de antecedentes de ambos equipos es un supuesto
exploratorio explícito y necesita validación prospectiva.

Se registra p exploratoria separada; `probability` permanece null y
`calibrated=false`. La validación prequential usa sólo antecedentes ya
disponibles antes del kickoff de cada evaluación: Brier, log loss, diferencia
de calibración y comparación contra baseline constante 0,5. No se ajustan
parámetros con el holdout del descubrimiento ni se declara calibración por
tener una métrica. No se estima EV. Incertidumbre Wilson corresponde al
conteo observado, no garantiza cobertura bajo cambios de régimen.

## Muestra y condiciones

El caso Arsenal original tiene sólo tres antecedentes locales compatibles al
ampliar la consulta de observaciones; esto no resuelve necesariamente el gate
de ambos equipos y localías del patrón original. Se conserva el bloqueo y el
historial exacto en `sample_review.json`. Hace falta más evidencia comparable
anterior al kickoff, especialmente del rival en la localía relevante.

Venue es observable; lag/context necesita agregación histórica congelada;
starter/posición/formación/técnico requiere contexto objetivo acreditado;
remates, faltas y marcador del propio partido son posteriores al inicio.
Clasificar una variable no la convierte en predictor validado. Se conservan
contraejemplos y ninguna transformación se ajusta sobre el holdout.

Las 69 ausencias de captura conservan el diagnóstico previo de los 73 fixtures:
40 no encontrados, seis en revisión, cuatro pendientes de horario, 19 fuera del
catálogo legado. No son fallos de modelos. No se inventó catálogo actualizado.

## Ejecución manual reproducible

```sh
.venv/bin/python -B tools/audit_commercial_frozen.py \
 --run outputs/control/runs/python_engine_real_v1 \
 --output-dir outputs/control/runs/python_engine_real_v1/commercial_manual_nuevo
.venv/bin/python -B -m unittest test_prematch_commercial_models
.venv/bin/python -B tools/run_offline_tests.py
```

El destino debe ser nuevo. La herramienta reutiliza los contratos del
mapeador, los preflights y el parser Bwin existentes. No inicia subprocesses
de adquisición. La herramienta depende de las rutas internas del expediente
congelado; no es todavía una etapa automática genérica de futuras jornadas.

## Riesgos y pendientes legítimos

Verificación: diez pruebas nuevas focalizadas superadas, incluyendo alias
femenino, separación de scopes, periodos, líneas, combinados, propietario,
ausencia, muestra, incertidumbre, deduplicación y fuga temporal. Suite offline
completa final ejecutada con protección de outputs originales; log local
`/tmp/prematch_objective3_final_tests.log`. `git diff --check` limpio.

No se cierra el objetivo como motor comercial completo: faltan calibración
prospectiva independiente, modelos de props con XI/exposición verificable,
liquidación de tarjetas, hándicaps y combinados, e integración automática
genérica del evaluador en jornadas futuras. Las 120 evaluaciones prueban el
recorrido contrato real → histórico → estimación → validación → expediente,
no autorización de apuesta. El precio sigue siendo snapshot histórico.
