# Auditoría offline de Navalcarnero y Armenia

Se utilizaron exclusivamente el inventario local y el catálogo congelado de
168 eventos. Las URLs aportadas por el usuario coinciden con las URLs locales;
el parámetro `market=-1` no interviene en la identidad. No hubo navegación,
scraping, API ni cambios comerciales.

## Resultados

| Medida | Antes (v7) | Después (v8) |
|---|---:|---:|
| Fixtures con candidato Bwin | 21 | 22 |
| Identidades verificadas offline | 6 | 9 |
| Capturas de cuotas verificadas | 4 | 4 |
| Sin candidato en catálogo | 52 | 51 |

### Navalcarnero–Real Betis

- Fixture API: `1644363`; Event ID: `7906410`.
- Equipos y orden: coincidencia exacta tras normalizar `CDA`.
- Hora: 12/11/2026 19:00 Europe/Madrid en API y cabecera PREMATCH.
- Competición: `Copa del Rey`, score de corroboración 1.0 en la inspección.
- Categoría API inicial: `UNKNOWN`, con `category_status=ELIGIBLE` y sin señales
  femeninas, juveniles o de reservas.
- Estado nuevo: `IDENTITY_VERIFIED_OFFLINE`.
- Evidencia categórica: `SENIOR_COMPETITION_CORROBORATED`.

El defecto consistía en exigir `M` o `F` explícito incluso cuando una
competición sénior exacta estaba corroborada por API y Bwin. La regla nueva no
deduce categoría de una puntuación de nombre: requiere simultáneamente ID y
nombre canónicos de una competición sénior registrada, competición Bwin con
score >= 0.82, fixture elegible y ausencia de señales de otra categoría.

La misma corrección afecta a dos casos equivalentes de Copa del Rey ya
presentes: Calvo Sotelo–Albacete (`7910678`) y Tarancón–Levante (`7910514`).
Ambos poseen equipos, orden, hora absoluta y competición corroborados. Por eso
el total de identidades verificadas aumenta de seis a nueve.

### Armenia–Chipre

- Fixture API: `1528959`; Event ID: `7909685`.
- El catálogo local ya contenía `Armenia Chipre`, pero `Cyprus` no generaba la
  traducción candidata `Chipre`.
- Se añadió el alias exacto de selección nacional dentro del contexto
  internacional. No es un alias global de clubes.
- El texto congelado no contiene hora y no existe inspección local de la página.
- Tampoco existe competición Bwin corroborada en el snapshot.
- Estado nuevo: `CANDIDATE_TIME_UNVERIFIED`; identidad no verificada.

La URL manual confirma dónde buscar en una inspección futura, pero no sustituye
la evidencia temporal y competitiva que falta en el snapshot PREMATCH.

## Controles conservados

El cierre por competición sólo se permite para identidades sénior exactas
registradas (`UEFA Nations League`, ID 5; `Copa del Rey`, ID 143). Exige que el
nombre canónico concuerde con el ID. No se aplica si:

- el evento muestra categoría femenina y el objetivo es desconocido;
- aparecen U19/U20/U21/U23, youth o reservas;
- la competición no está corroborada;
- la hora falta o contradice API;
- los equipos, orden o Event ID no son inequívocos.

No se modificaron umbrales de apuestas, modelos, cuotas ni mercados.

## Artefactos

La matriz actualizada está en
`outputs/control/runs/python_engine_real_v1/bwin_reconciliation_v8/`:

- `reconciliation_73.json`
- `reconciliation_73.csv`
- `summary.json`

Se mantiene el comando offline del informe del Objetivo 4; la carpeta destino
debe ser nueva. Para Armenia puede usarse `--fixture-id 1528959`, y para
Navalcarnero `--fixture-id 1644363`.

Verificación final: 692 pruebas, cero fallos, cero errores y diez omisiones
documentadas; red y outputs originales protegidos. `git diff --check` limpio.
