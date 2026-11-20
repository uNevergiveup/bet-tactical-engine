# Objetivo 7.1 — aislamiento de evaluaciones diarias

## Diagnóstico

La protección `write-once` funcionaba correctamente. La colisión ocurría antes:
una ventana civil, por ejemplo `2026-11-12_1500_0300`, utilizaba siempre las
mismas rutas `fixtures.json`, `fixtures_major.json` y `prospective_trial/`.
Una ejecución posterior podía tener otro instante de referencia, horizonte,
presupuesto o conjunto elegible y terminaba intentando publicar un segundo
`fixtures_major_test.json` distinto en el mismo destino. El error
`FROZEN_TRIAL_INPUT_WRITE_ONCE_CONFLICT` evitaba la sobrescritura, pero no
existía una identidad de evaluación anterior a la escritura.

## Almacenamiento corregido

Las ejecuciones nuevas conservan la carpeta de ventana como contenedor y crean
automáticamente una evaluación aislada:

```text
outputs/discovery/<VENTANA>/
  evaluations/
    <UTC_REFERENCIA>_<HASH_SOLICITUD>/
      evaluation_manifest.json
      fixtures.json
      fixtures_major.json
      prospective_trial/
        fixtures_major_test.json
        candidate_ledger.json
        enriched/
        daily_runs/
```

`evaluation_manifest.json` fija la referencia Madrid/UTC y una representación
canónica de ventana, horizonte, presupuestos, profundidad histórica, tamaños de
lote, filtro de equipos, ámbito de competición y margen PREMATCH. Su hash se
incorpora también al handoff.

Los layouts históricos planos `outputs/discovery/<VENTANA>/prospective_trial`
no se migran ni modifican. Los lectores usan la evaluación aislada más reciente
si existe y, en caso contrario, mantienen el layout histórico. Los artefactos
globales de los Objetivos 5.1, 6 y 6.1 no cambian.

## Nueva evaluación frente a reanudación

- Una evaluación terminada o con handoff terminal nunca se reutiliza para una
  ejecución nueva. Un `TECHNICAL_FAILURE` de preparación sí permite reanudar
  los checkpoints intactos; un fallo posterior a `PREPARED` no mezcla runs.
- Un cambio de horizonte, presupuesto, profundidad, ámbito o ventana genera
  otra identidad y otro directorio.
- Solo se reanuda automáticamente un ensayo inacabado con la misma solicitud,
  `fixtures_major_test.json`, checkpoints de enriquecimiento y cadena completa
  de hashes todavía intacta.
- Si un input congelado fue modificado, la reanudación se bloquea con
  `RESUME_FROZEN_SOURCE_IDENTITY_MISMATCH`; no se crea silenciosamente una copia
  que oculte la alteración.

La carpeta se resuelve bajo el lock diario antes de ejecutar descubrimiento o
escribir `fixtures.json`, `fixtures_major.json` o `candidate_ledger.json`.

## Tiempo de referencia

El comando captura un único instante de referencia y lo transmite a:

1. descubrimiento y filtrado temporal de fixtures;
2. selección por horizonte y margen;
3. adquisición histórica/preselección;
4. descubrimiento de patrones;
5. motor táctico y ranking estadístico.

Una reanudación conserva la referencia original del snapshot. Las
comprobaciones de seguridad al finalizar usan además el reloj real y solo
pueden retirar un encuentro que ya no tenga margen; nunca incorporan otro que
no perteneciera al conjunto congelado.

## Validación offline

`test_prematch_objective7_1.py` cubre:

- dos evaluaciones terminadas de la misma ventana iniciadas a horas distintas;
- cambios simultáneos de horizonte y presupuesto;
- reanudación del mismo snapshot y conservación de su hash;
- rechazo de una modificación en la cadena congelada;
- ausencia de sobrescrituras y lectura compatible del layout histórico.

La suite completa se ejecutó sin API-Football ni Bwin: 718 pruebas correctas,
10 omitidas. No se modificaron filtros estadísticos, criterios comerciales,
umbrales ni generación de picks.

## Comando diario

El comando del Objetivo 7 no cambia y no requiere un ID manual:

```bash
.venv/bin/python -B prematch_daily.py \
  --online --confirm-external --window normal \
  --history 20 --details 8 --discovery-retries 1 \
  --bwin-identity-catalog \
  outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_1_v1/reconciliation_73.json
```

El identificador de evaluación se genera antes de cualquier escritura de
entrada congelada.
