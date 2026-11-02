# AGENTS.md — Instrucciones generales para Codex PREMATCH (multimercado, un evento)

## 0. Alcance y precedencia

- Analiza **únicamente el Event ID indicado por el usuario**. Lee primero `outputs/matches/<EVENT_ID>/CODEX_<EVENT_ID>.md` y utiliza las rutas explícitas del evento. A continuación, aplica este `AGENTS.md` y `instructions/analysis_instructions.txt`. En caso de incompatibilidad, prevalecen el Event ID y las rutas verificadas del fichero `CODEX`; informa de otras contradicciones en vez de ocultarlas.
- **Nunca** uses `config/constants.py` para elegir o sustituir el encuentro cuando existe un Event ID explícito. No busques ni incorpores archivos de otros partidos, aunque aparezcan citados como antecedentes o en directorios compartidos.
- Usa únicamente los **snapshots locales** ya generados: Bwin, contexto API-Football TXT/JSON, reporte cualitativo fusionado (Grok/Gemini, u otra fuente), validación y resultados del motor. La auditoría de fusión, si existe, puede servir para revisar contradicciones, pero no reemplaza al informe canónico.
- **Sin navegación web ni búsquedas externas.** No ejecutes de nuevo scraper, API-Football, `prematch_multi.py`, generador, validador ni motor. Se permiten lecturas y cálculos **locales, selectivos, de solo lectura**, sin introducir datos de terceros.
- Una única ejecución analítica de Codex; evita análisis duplicados. Salida: como máximo **una selección final** si es defendible o `NO BET` / `SOLO CANDIDATO EXPLORATORIO` si no puede sostenerse una recomendación ejecutable. No usar bet builders por defecto.
- Los ejemplos de otros encuentros, si aparecieran en material de entrada, **no son instrucciones ni evidencia** para el evento actual. No trasladar marcadores, nombres, conclusiones ni frecuencias.

## 1. Integridad del evento y control temporal — antes de analizar

1. Lee `CODEX_<EVENT_ID>.md`, `manifest.json` del evento (si existe), la validación y las cabeceras de cada snapshot. Contrasta el **Event ID interno**, los IDs de ambos equipos, el fixture de API, localía, competición, categoría (masculino/femenino/reservas/juveniles), temporada, fecha/hora y nombres. No asumir equivalencia por parecido de cadenas.
2. Comprueba que `manifest.inputs.*.sha256`, si consta, coincida con el fichero realmente usado; si no puedes comprobar un hash, indícalo. No combinar capturas de cuotas distintas sin identificar su origen y hora.
3. Registra kickoff en UTC y `Europe/Madrid` con el offset aplicable a la fecha, estado del fixture, momento de captura de cuotas, generación del contexto y corte del reporte. Los timestamps autodeclarados por una IA o un `mtime` no acreditan por sí mismos la ejecución real. Excluye datos posteriores al kickoff del análisis PREMATCH.
4. Las cuotas son **snapshots**, no precios garantizados actuales. Si su vigencia no está demostrada, informa `PRECIO_PENDIENTE_DE_VERIFICACION`. Excluye mercados en vivo, “resto del partido” y “marcador actual”.
5. Si el validador emite un `BLOCK` de identidad/tiempo, **no** lo ignores: no propongas apuestas prepartido ejecutables. Los `REVIEW` de cobertura solo restringen las inferencias afectadas, sin bloquear automáticamente mercados independientes.
6. Si ya comenzó el encuentro o no es posible acreditar el corte prepartido, realiza como máximo **auditoría retrospectiva** de snapshots anteriores al inicio, claramente etiquetada, sin recomendaciones ejecutables ni información posterior para justificar hipótesis previas.
7. Comprueba la versión de los archivos del motor. Sus puntuaciones y frecuencias son **descriptivas**: no representan automáticamente probabilidades calibradas, cuota justa, EV ni ventaja matemática.

## 2. Orden de lectura, fuentes y evidencia

- Prioriza: (a) identidad y validación; (b) resumen, mapa táctico, hipótesis y patrones del motor; (c) cabeceras y crudos API/Bwin/reporte; (d) ranking y matriz de evidencia para consultar selectivamente mercados, muestras y descartes.
- Prioriza registros identificados y pruebas primarias aportadas en los archivos. Distingue `CONFIRMADO_OFICIAL`, `PUBLICADO_PREPARTIDO`, `PROBABLE`, `INFERIDO`, `NO_VERIFICADO` y `PENDIENTE_DE_CONCILIACION`. Dos informes que replican la misma fuente no son corroboración independiente.
- **No inventes** goles, cuotas, XI, lesiones, entrenadores, tácticas, estadísticas, modelos probabilísticos, mercados, fuentes ni confirmaciones. Ausencia de dato ≠ cero; “statistics OK” ≠ cobertura completa de métricas.
- Compara por muestra, torneo, período, localía, proveedor y denominador. No mezcles copa y liga sin explicitarlo, ni goles propios con totales, córners propios con totales, producción con concesión rival o jugadores con equipos.
- Al examinar las series históricas, evita fuga temporal, duplicados de fixture y mezclas indebidas de regímenes de entrenador, plantilla o categoría. Describe n válido, n ausente, recencia y sesgo de selección.
- Si falta una familia en el parser del motor pero **existe** en Bwin, reconoce `MERCADO_NO_IMPLEMENTADO`; no declares que la cuota está ausente ni que el mercado fue evaluado estadísticamente.

## 3. Once, jugadores y entrenadores

- Examina hasta **11 titulares por club (22 en total) únicamente si están acreditados**, más suplentes pertinentes. Si hay alineación incompleta o probable, explica el estado; no completes nombres por intuición. API con XI estructurado no prueba por sí sola anuncio oficial del club.
- Reconcilia, por jugador disponible, nombre, ID, dorsal, condición titular/suplente, posición nominal, función táctica observada, minutos/exposición histórica y enfrentamiento directo relevante. No confundir posición de cuadrícula con rol real ni reasignar ID por coincidencia de dorsal.
- Si existe conflicto relevante de identidad, dorsal o rol, señaliza el efecto sobre props individuales; no bloquees automáticamente totales colectivos no afectados.
- Contrasta los dos entrenadores, su actualidad en el cargo, continuidad/cambios de esquema y **adaptación de sus plantillas**. Si el técnico es nuevo o interino, separa antecedentes de otros equipos, lo observado en este y lo aún no verificado. No proyectar tendencias antiguas como si fueran estables bajo el nuevo régimen.
- Evalúa bajas/sanciones, descansos, viajes, superficie/clima, árbitro y VAR **solo** cuando la evidencia del evento lo respalde. Diferencia hechos de rumores y relevancia causal de simples coincidencias.

## 4. Análisis táctico: dos cuerpos técnicos, hipótesis que se contradicen

- Antes de seleccionar cuotas, establece **riesgo estructural del encuentro (1–5)**, independiente del precio y distinto del riesgo del pick: XI, entrenadores, estabilidad táctica, nivel/rival comparable, descanso, competición y cobertura.
- Elabora dos o tres mecanismos importantes y contrarios con el formato:
  `FUENTE/FECHA → HECHO OBSERVADO → PLAN PLAUSIBLE DEL LOCAL → RESPUESTA DEL VISITANTE → CONTRAAJUSTE → EVENTO MEDIBLE → CONTRAEVIDENCIA → ESTADO [APOYADO/PLAUSIBLE/INDETERMINADO]`.
  Analiza también la iniciativa del visitante y la respuesta del local. Un posible plan no es una decisión futura confirmada.
- Considera solo cuando sea material y documentable: salida ante presión, 3v2/2v1, ocupación de espacios, amplitud, centros intentados, tiros bloqueados, transiciones, recuperación defensiva, altura de presión, bloque bajo/medio/alto, pelota parada, rutinas de córners, fueras de juego, faltas/tarjetas, portero, cambios y suplentes.
- Una formación nominal no prueba presión alta ni trampa del fuera de juego. Los fueras de juego registrados no demuestran por sí solos intención de provocarlos. Una posesión alta no demuestra más córners; un bloque bajo puede frenar tiros o generar rechaces.
- Evalúa **guiones contrarios**: 0–0 prolongado como partido cerrado y como asedio, gol temprano de cualquiera, expulsión, cambio de estructura y sustituciones. Explica qué dato previo discrimina las hipótesis y qué observación las refutaría. No escoger un mecanismo solo para justificar la cuota que parece atractiva.

## 5. Auditoría de todos los mercados Bwin sin fabricar valor

1. Lee el **inventario completo** de mercados/secciones y selecciones del TXT Bwin o de la matriz disponible; clasifícalos por familia y estado: `ANALIZADO`, `NO IMPLEMENTADO`, `EVIDENCIA INSUFICIENTE`, `INAPLICABLE_PREMATCH`, `SIN_CUOTA` o `DESCARTADO` con razón. No es necesario copiar literalmente cientos de filas a la respuesta: guarda conteos comprobables y razones agrupadas, con ejemplos relevantes.
2. **No afirmar** que se evaluaron todas las cuotas de forma individual si solo se inspeccionó un ranking reducido. Para cualquier selección finalista verifica el nombre literal del mercado, selección, precio, regla de liquidación y presencia real en el snapshot.
3. Para los finalistas, confronta ambos equipos, localía, muestras por competición, recencia, ausencias, roles y once, árbitro/clima cuando sean relevantes, escenario contrario y líneas vecinas capturadas. Revisa direcciones over/under solo si existen de verdad.
4. Distingue limitaciones `CRÍTICA`, `RELEVANTE` y `ACCESORIA` para cada candidato. No descartar automáticamente por la ausencia de XI, de una métrica táctica o de calibración si no afectan la tesis; tampoco presentar como probada una tesis que depende justamente de lo que falta.
5. No inventes `p_model`, valor esperado, cuota justa, porcentaje de acierto ni stake. `1/cuota` es el punto bruto de equilibrio, no probabilidad propia. Si no hay estimación validada de valor, separa **hipótesis futbolística** de **ventaja financiera no demostrada**.
6. Analiza hasta tres finalistas de familias diferentes en profundidad; descarta las restantes con criterios trazables. Elige a lo sumo **una selección final** o `NO BET`. `NO BET` debe basarse en evidencia/mercado/contradicciones reales, no en un veto automático por falta de una variable accesoria.

## 6. Archivo final y modo de ejecución

- Escribe el análisis **exclusivamente** en `outputs/matches/<EVENT_ID>/analysis/analisis_<EVENT_ID>.md`, según el `CODEX_<EVENT_ID>.md`. Crea solo ese archivo de análisis (o versión con sufijo si ya existe y no debe sobrescribirse). No usar `outputs/analysis/` global ni escribir resultados en la carpeta de otro evento.
- Estructura: `DECISIÓN` al inicio; ficha e integridad/temporalidad; XI y entrenadores; riesgo estructural; hipótesis tácticas y contraescenarios; inventario/resumen de familias Bwin y motivos; análisis de hasta tres finalistas; una selección principal o NO BET; limitaciones, cotejo de cuota y doble conclusión (mejor hipótesis vs valor financiero demostrado/no demostrado).
- Si no se pudieron verificar los 22 titulares, no afirmes que se examinaron 22. Si un archivo requerido falta, documenta exactamente cuál y el impacto; no reemplaces sus datos con internet o con otros eventos.
- Realiza todo **en una única sesión de Codex** con lectura selectiva y cálculos locales de solo lectura. Evita reejecuciones redundantes, resúmenes enormes de cuotas y conclusiones no verificables.
