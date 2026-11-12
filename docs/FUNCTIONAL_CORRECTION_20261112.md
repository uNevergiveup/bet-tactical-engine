# Corrección funcional PREMATCH: registro de trabajo

Estado vigente: corrección funcional completada y verificada offline. **NO READY
para lanzar directamente la ejecución real completa**: el siguiente paso operativo
es descubrimiento controlado con autorización de red. No se ha adquirido dato real.
Los estados EN CURSO de los bloques siguientes son el historial del trabajo;
la auditoría de cierre al final establece el resultado vigente.
Rama: `codex/prematch-functional-isolation`, desde `bf81f0fe`.
Ventana objetivo: 2026-11-12 15:00 CET → 2026-11-13 03:00 CET.

## Bloque implementado

| Defecto | Corrección | Límites pendientes |
|---|---|---|
| `latest` permite consumir enlaces del proyecto anterior | Rechazo de punteros globales en resolución local, carga single y generadores de prompts; release exige selección explícita | No se modifican los enlaces. Quedan lectores adicionales por revisar. |
| Rutas absolutas restauradas salen del proyecto actual | `prematch_paths.local_reference`: remapeo de raíz conocida, límite resuelto de directorio, hash obligatorio al remapear; integración en runtime, single y manifiestos multi | La migración de lectores todavía no es universal; no se reescriben JSON originales. |
| `mtime` de restauración implica frescura | Nuevos registros context-acquisition-v1 con timestamp de adquisición y hash de payload; lectores de contexto y compatibilidad de Fase 1 dejan de utilizar mtime | Registros legacy sin timestamp no se reutilizan como actuales. Sus crudos permanecen para auditoría histórica. Revisar consumidores adicionales del nuevo formato. |
| Instrucciones ausentes | Recuperación byte por byte desde release restaurada, con excepción explícita en .gitignore | Prevalecen Event ID, CODEX y AGENTS actuales sobre posibles referencias históricas del texto. |

Fuente de instrucciones: `outputs/releases/v0.1.0-rc1/source_snapshot/instructions/analysis_instructions.txt`.
Hash cotejado con `SOURCE_MANIFEST.json` de esa release:
`524c7fd668c62a3a4811e0c70e69f8a79dd236f0c8ba44288465fc8fa72977f3`.
No se añaden ni modifican reglas comerciales.

## Verificación

- 119 pruebas focalizadas: OK.
- Suite global modificada: 597 pruebas, 1 fallo, 19 errores, 10 omitidas.
- Baseline del commit de partida en copia temporal: 591 pruebas, 1 fallo,
  19 errores, 9 omitidas. Los 20 identificadores de fallo/error coinciden.
- La copia baseline se construye a partir de Git; sus outputs enlazan en lectura
  al árbol restaurado. Las diferencias de ruta de la copia limitan la comparación.
- Ejecución global protegida por audit hooks: bloqueo de conexiones de red,
  escritura/mutaciones en outputs originales y proyecto antiguo; bloqueo de
  subprocesos reales de scraper, generador y multi.
- Las pruebas focalizadas utilizan fixtures locales y mocks; no adquisición real.
- `git diff --check`: OK.

Logs de la sesión: `/tmp/prematch-functional-focused.log`,
`/tmp/prematch-functional-global-final.log`, `/tmp/prematch-functional-baseline.log`.
Los logs temporales no son artefactos duraderos de release.

Fallos pendientes: expediente 7902458 ausente (integral/inteligencia táctica),
CSV histórico ausente, claves absolutas antiguas en manifiesto de conciliación,
contrato Bwin que compara snapshots de ambos árboles, reuse de cuotas con
referencias antiguas aún no normalizadas en todos los niveles, navegador
Playwright esperado por la prueba de DOM no disponible para esa versión.
No se omiten ni convierten en éxitos estos fallos.

## Requisitos todavía abiertos

1. Completar resolución segura y propagación de referencias en review, scanner,
   mapping y fuentes anidadas; mantener contratos de identidad y hashes.
2. Registrar la dependencia real de cada familia/modelo respecto a XI, roles,
   entrenador y formación objetivo. El predictor congelado no implementa una
   corrección probabilística por cambio de XI.
3. Implementar delta previsto/confirmado acreditado antes de kickoff, invalidación
   y recálculo selectivo, conectado a actualización diferencial de contexto.
4. Verificar reevaluación de precios tras refresh-odds y bloquear reutilización
   de artefactos físicamente conservados cuyo binding ha quedado invalidado.
5. Resolver o aislar mediante fixtures reproducibles los fallos globales y volver
   a ejecutar focalizadas/global. No alterar los expedientes originales.

Preparación actual: solamente prueba controlada de descubrimiento; ejecución
completa NO READY. No se ha iniciado la jornada ni se han consumido API/cuotas.

## Segundo bloque: XI diferencial y reevaluación

Implementado en `prematch_context_delta.py`, integrado en single, multi, motor y
release. Este bloque no resuelve todavía todos los requisitos abiertos anteriores.

| Consumidor | Dependencia real | Tratamiento |
|---|---|---|
| Motor, props de jugador | Identidad resuelta, titular/suplente e históricos | La clave de evidencia incluye jugador resuelto y sus datos objetivo; recalcula al cambiar. |
| Motor, evidencia colectiva | Series históricas, equipos, métrica y línea | Conserva cálculos únicamente si todas esas dependencias y el código coinciden. |
| Mapas/hipótesis, XI quality, patrones de jugadores | Contexto objetivo, roles nominales, formación, entrenador | Se reconstruyen en finish; no reutiliza el análisis ni la evaluación final anterior. |
| Predictor congelado | Observaciones históricas, cutoff y código del predictor | No se inventa un ajuste numérico por XI. Su baseline no se presenta como probabilidad condicionada al once actual. |
| Precio/ranking/fragilidad y filtro de cuotas | Snapshot actual, línea y reglas existentes | Se reconstruyen siempre. La caché guarda solo evidencia descriptiva. |

`refresh-context` incorpora exclusivamente un registro local del endpoint
`fixtures/lineups` con SHA y adquisición anterior al kickoff, posterior al contexto
base y no futura respecto al reloj real. Comprueba fixture, equipos, IDs únicos,
ambos XI completos y coherencia JSON/TXT. Compara altas/bajas, posiciones nominales,
grid, formación y entrenador. Una cuadrícula no acredita función táctica real.
El baseline antiguo puede estar incompleto; no se completan jugadores por intuición.

La salida está en `outputs/matches/<event>/context_updates/<hash>/`: nuevos JSON,
TXT e identidad, delta, adquisición original y copia del manifiesto padre.
Conserva los históricos y su generated_at original; registra por separado la nueva
adquisición. Copia los artefactos derivados anteriores antes de publicar el nuevo
manifiesto. Invalida validación, motor, análisis y revisión final. No borra archivos.

La captura directa del endpoint de XI en `generate_context.py` registra ahora
evidencia append-only bajo `outputs/data/cache/acquisitions/fixtures_lineups/`,
incluso cuando se omite la caché reutilizable. No se ha ejecutado esa captura real.

Ejemplo para una ejecución futura, con selección y adquisición nuevas verificadas:

```bash
.venv/bin/python -B prematch_single.py \
  --selected outputs/matches/<event>/selection/<run>/selected_match.json \
  refresh-context \
  --snapshot outputs/data/cache/acquisitions/fixtures_lineups/<hash>.json \
  --snapshot-sha256 <sha256_del_registro>
```

No obtiene datos ni ejecuta finish. La actualización deja el evento PREPARADO,
borra indicadores de éxito antiguos de validación/motor y requiere finish.
Un API XI estructurado sigue etiquetado `API_STRUCTURED_NOT_OFFICIAL_CERTIFICATION`:
no certifica anuncio oficial de club ni una función táctica observada.

Multi vincula por SHA una caché de evidencia descriptiva entre evaluaciones.
Cambios en código, históricos, métrica, línea o identidad invalidan las entradas
afectadas. Los precios y puntos brutos de equilibrio se reconstruyen fuera de
esa caché. No se calcula p_model, EV ni stake.

Tras refresco de contexto o cuotas, release usa un binding de análisis versionado
por SHA del manifiesto actual. La revisión final no acepta el binding anterior,
aunque el fichero se conserve físicamente. Sin nueva validación/motor el estado
PREPARADO impide emitir entrada final.

Pruebas del bloque: 104 focalizadas OK; regresiones sintéticas para identidad,
fechas, XI parcial/duplicado, preservación de originales, integración single,
cambios nominales/formación/entrenador, caché selectiva por jugador, línea vecina,
precio cambiado, exclusión live y separación del binding anterior.
Logs: `/tmp/prematch-delta-focused.log`, `/tmp/prematch-delta-global.log`.

Sigue pendiente probar el ciclo integral finish → motor → análisis → revisión
con fixtures autocontenidos, completar portabilidad de scanner/mapping y resolver
la suite global histórica sin alterar originales. Por ello NO READY; objetivo
activo, sin bloqueo externo que impida seguir corrigiendo.

## Tercer bloque: portabilidad y suite autocontenida

- Scanner y selector resuelven snapshots y fuentes de tiempo contra límites
  deducidos de controles locales o contra el root explícito del runtime.
- Los lectores operativos de JSON rechazan `outputs/latest` y controles absolutos
  del proyecto anterior. Las referencias legacy anidadas sólo pueden remapearse
  mediante SHA; no se abre el original externo como fallback.
- Mapping concilia claves antiguas de `INTEGRITY_MANIFEST.json` en memoria,
  comprueba copia local y detecta claves actuales/antiguas con hashes conflictivos.
- Release verifica también las referencias anidadas de selección y binding final
  dentro del root. La auditoría de cobertura remapea lotes por hash y no lee
  respuestas de caché con referencia no verificable.
- No se reescriben los manifiestos históricos para conseguir que pasen las pruebas.

Fallos históricos resueltos en los tests:

1. Integral/inteligencia táctica: fixtures sintéticos temporales con métricas,
   mitades, ausencias y denominadores explícitos; no reconstruyen el evento real.
2. CSV de 162 filas: genera una jornada sintética con duplicados y verifica el
   exportador real, en lugar de exigir un CSV ausente de otra instalación.
3. Reuse/Bwin: copia temporal del snapshot archivado cuyo SHA corresponde a la
   selección congelada. La captura canónica fue refrescada después y debe seguir
   rechazándose para esa selección antigua.
4. Test DOM: usa Chromium local disponible si falta el binario esperado por la
   versión de Playwright. Sólo HTML en memoria y todas las rutas abortadas.

Verificación final de este bloque:

```bash
.venv/bin/python -B tools/run_offline_tests.py
```

Resultado: **609 pruebas, cero fallos, cero errores, 10 omisiones explícitas**.
El resumen JSON del lanzador enumera cada omisión y su causa. No equivale a
comprobar los expedientes históricos opcionales que faltan.
Focalizadas de portabilidad/modelos: 80 OK; release/delta adicional: 29 OK.
Log global: `/tmp/prematch-guarded-global.log`.

El lanzador bloquea conexiones Python y subprocesos de adquisición, y protege
outputs y el proyecto antiguo contra escrituras/movimientos/borrado. Los hijos
Python heredan el guard mediante sitecustomize temporal. Es una defensa frente
a efectos accidentales de las pruebas, no un sandbox para código hostil.

Continúa pendiente la integración autocontenida del ciclo completo de finish y
revisión final tras refrescos sucesivos. Los tests actuales verifican las piezas,
pero todavía no acreditan ese recorrido completo con el validador y motor reales.
Por ese límite, el objetivo sigue activo y la preparación sigue siendo sólo para
descubrimiento controlado; NO READY para ejecución completa.

## Cierre: ciclo completo y auditoría de requisitos

Se ha completado el pendiente de integración con `test_prematch_final_cycle.py`.
Las únicas sustituciones son el reloj y el transporte de procesos/adquisición:
se invocan el validador y motor reales, sus parsers, cálculos y generadores de
artefactos; no se simulan resultados de validación ni rankings. El evento y las
fuentes son sintéticos y temporales; no se generan recomendaciones reales.

El recorrido comprobado es:

1. Single → multi.finish_one → validador real → motor real → binding → revisión NO BET.
2. Nuevo XI local → delta → invalidación → rechazo del análisis anterior → finish
   real → recálculo de la evidencia del jugador afectado y reutilización colectiva.
3. Refresco de cuotas simulado en el transporte → captura 2,00 a 1,50 → invalidación
   → finish real → precio y punto bruto de equilibrio 0,66667 reconstruidos → nueva
   revisión. La caché de evidencia se reutiliza sin reutilizar el precio anterior.
4. Snapshot posterior al kickoff → BLOCK del validador real, sin ejecución del motor.
   Reloj posterior al kickoff → rechazo antes de iniciar ambas etapas.

Hallazgos adicionales corregidos:

- Finish eliminaba la copia de bandeja de entrada y podía sustituir el informe
  canónico anterior. Ahora conserva el original y fija revisiones distintas bajo
  `inputs/report_versions/<sha256>/`, sin sobrescribir versiones anteriores.
- El último uso operativo de mtime en captura Bwin se sustituye por fecha declarada
  del TXT, Event ID, URL y pertenencia al intervalo de adquisición. Una restauración
  reciente con cabecera antigua se rechaza; un mtime antiguo con captura nueva
  acreditada no se convierte por sí solo en veto.
- IDs booleanos no se aceptan como IDs de equipo numéricos en un delta de XI.

### Evidencia requisito por requisito

| Requisito | Evidencia de cierre |
|---|---|
| Aislamiento de latest/ACTIVE_EVENT | Lectores operativos rechazan esos controles; regresiones de los tres enlaces y de jornada anterior. Los enlaces originales no se modifican. |
| Estado nuevo 12/11 | Status real NO_DAILY_HANDOFF, sin preselección ni carpeta diaria; dry-run confirma 12/11 15:00 → 13/11 03:00 CET (14:00 → 02:00 UTC). |
| Portabilidad e integridad | Resolver local con hash y límite resuelto; pruebas de traversal, symlinks externos, claves conflictivas, controles legacy y fuentes manipuladas; mapping y selección históricos pasan en copias locales. |
| Temporalidad y caché | Generador/Fase 1 usan adquisición registrada; legacy sin timestamp no recibe frescura del filesystem. Regresión Bwin header/mtime y exclusión de snapshots futuros/postkickoff. Históricos permanecen intactos. |
| Instrucciones | Copia exacta de release y SHA comprobado 524c7fd668c62a3a4811e0c70e69f8a79dd236f0c8ba44288465fc8fa72977f3; rutas de consumo y excepción Git verificadas. |
| Dependencias de XI | Tabla por modelo en segundo bloque; delta de IDs/posición nominal/grid/formación/coach; integración single y motor con recálculo selectivo real. |
| Cuotas y artefactos | Prueba integrada tras refresh-odds: nuevo precio, evidencia reutilizada, viejos bindings rechazados, archivos previos conservados y nueva revisión requerida. |
| Sin reglas inventadas | Parámetros/umbrales comerciales permanecen iguales. La caché envuelve evidence_for; no añade p_model, EV, stake ni ajustes por titularidad. El resultado de prueba es NO BET. |
| Pruebas y límites de red | Lanzador offline: 615 pruebas, 0 fallos, 0 errores, 10 omisiones explícitas. Focalizadas finales: 63 OK; ciclo real con cinco regresiones: OK. Red/adquisición real bloqueadas. |
| Reversibilidad y conservación | Rama codex/prematch-functional-isolation, commits separados; git diff --check OK. Inventario final 48.093 archivos, 3.753.111.345 bytes y los mismos tres enlaces; guard de escritura sobre originales durante pruebas. |

Comandos reproducibles de verificación:

```bash
.venv/bin/python -B tools/run_offline_tests.py
.venv/bin/python -B -m unittest test_prematch_final_cycle
.venv/bin/python -B prematch_daily.py --status --date 2026-11-12
.venv/bin/python -B prematch_daily.py --date 2026-11-12 --dry-run
.venv/bin/python -B -m pip check
```

Log final global: `/tmp/prematch-final-global.log`. Las diez omisiones figuran en
el resumen JSON del lanzador: snapshots históricos opcionales ausentes y una prueba
de falta de credencial omitida porque existe credencial configurada. No se descargan
fixtures ni se consideran esas omisiones como integraciones históricas verificadas.

### Archivos modificados

- Rutas/estado: `prematch_paths.py`, `prematch_daily.py`, `prematch_runtime.py`,
  `prematch_single.py`, `prematch_multi.py`, `prematch_release.py`,
  `select_single_match.py`, `prematch_opportunity_scanner.py`,
  `prematch_market_mapping_v1.py`, `generar_prompt_grok.py`, `generar_prompt_gemini.py`.
- Cachés/adquisición: `prematch_cache_record.py`, `generate_context.py`,
  `prematch_phase1.py`, `bwin_market_preflight.py`, `prematch_history_recovery_v2.py`,
  `prematch_tactical_history.py`.
- XI/evaluación: `prematch_context_delta.py`, `motor_probabilistico.py`.
- Instrucciones/documentación: `.gitignore`, `instructions/analysis_instructions.txt`,
  este informe.
- Pruebas/fixtures: `prematch_test_fixtures.py`, `test_prematch_final_cycle.py`,
  `test_prematch_functional_isolation.py`, `test_prematch_context_delta.py`,
  `test_prematch_bwin_phase3.py`, `test_prematch_comparison_v2.py`,
  `test_prematch_integral_analysis_v1046.py`, `test_prematch_tactical_intelligence_v1050.py`,
  `test_prematch_phase1.py`, `test_prematch_pilot_aliases.py`,
  `test_prematch_reuse_odds_contract.py`, `test_prematch_temporal_identity_contract.py`,
  `tools/offline_test_guard.py`, `tools/run_offline_tests.py`.

### Preparación real y límites pendientes

No quedan fallos internos conocidos de los contratos corregidos en la suite
ejecutada. El flujo completo ha pasado offline, pero no se ha comprobado conectividad,
entitlement/cuota real API, disponibilidad Bwin ni cobertura actual de la jornada.
Tampoco existen aún calendario conciliado, precios vigentes o XI de un evento nuevo.
Una alineación API no se certifica automáticamente como anuncio oficial del club.
Cuando una tesis depende de esa oficialidad, sigue requiriendo evidencia acreditada;
el sistema no la inventa ni convierte datos de prueba en una confirmación.

Por ello, **el siguiente paso real es únicamente descubrimiento controlado**, no
iniciar directamente el pipeline completo ni declarar READY. Ejemplo de comando
propuesto, NO ejecutado y que requiere autorización explícita de adquisición:

```bash
.venv/bin/python -B prematch_phase1.py \
  --date 2026-11-12 --start 15:00 --end 03:00 \
  --online --discover-bwin --discovery-only --max-api-calls 2 --bwin-headless
```

La fase de corrección autorizada termina sin consumir API, scraping real, análisis
de apuestas ni modificación de los históricos originales. La prueba real posterior
es una operación distinta, todavía no realizada ni autorizada por este informe.
