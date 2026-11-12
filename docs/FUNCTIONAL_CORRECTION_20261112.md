# Corrección funcional PREMATCH: registro de trabajo

Estado: EN CURSO. No certifica READY ni autoriza adquisición real.
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
