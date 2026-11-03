# Motor descriptivo táctico-estadístico V1

No predice decisiones de entrenadores, probabilidades calibradas, valor económico, EV ni stake. Una señal sustentada permite investigar una familia incluso si goles o resultado son muy variables. Las fórmulas antiguas de preselección y Pattern Engine se conservan.

## Datos y cortes

El motor usa preselection.json y Pattern Engine vinculados por SHA, y observaciones de nuevas preselecciones (sin consultas adicionales). Para snapshots antiguos reconstruye fixtures **FT, misma liga y temporada**, con IDs de equipo acreditados en fixtures_major_test.json, desde caché local API-Football cuya descarga no sea posterior al instante de evaluación. Filtra fechas anteriores tanto a analysis_cutoff como al kickoff; deduplica fixture y conserva hasta 30 (20 por defecto desde caché). AET/PEN no se mezclan con tiempo reglamentario.

La caché de partidos terminados se usa como evidencia histórica, sin afirmar que sea contexto actual. Cada archivo usado lleva SHA, y se comprueba que no cambie durante el cálculo. No se considera evidencia disponible antes de su descarga. Para reproducir el pasado usar --asof con offset: toda salida queda RETROSPECTIVE_AUDIT, selected_fixture_id=null y sin handoff accionable.

Si solo hay agregados, se conservan n/medias/CV, sin fabricar recencia, datos por sede o sensibilidad puntual. Se etiqueta AGGREGATED_PRESELECTION; confianza limitada. Los crudos posteriores al corte se excluyen. Un fixture ya iniciado se bloquea incluso al reproducir otro instante posterior.

Cada serie aporta n, denominador, ausencias, media, desviación/CV, localía, últimos cinco vs cinco anteriores cuando hay al menos tres en cada tramo, y rango de medias leave-one-out. Cobertura = n/partidos cuyo detalle fue solicitado/disponible; se separa explícitamente el historial no detallado por presupuesto (history_not_detailed_n) de una métrica faltante dentro del bloque solicitado. Goles usa todo el historial comparable. Ceros observados son válidos, null no es cero. Tiros bloqueados se leen cuando existen, sin modificar los pesos anteriores.

## Familias y límites

Se cruzan producción propia y concesión del rival por lado: goles, córners, tiros, tiros a puerta, faltas, amarillas, offsides y xG observado. xG solo apoya goles; no crea una familia independiente ni se estima si falta. Para córners se muestran asociaciones de Pearson con tiros/bloqueados sobre fixtures comunes (n>=4, varianza no nula): asociación no demuestra presión, juego por bandas o causalidad. No se infiere eficacia de córners ni goles a balón parado.

Concesión de faltas = faltas cometidas por el rival en el mismo partido; no acredita faltas recibidas por un jugador. Offsides provocados se reconstruyen de los offsides del oponente en esos fixtures; no prueban altura de línea. Amarillas no equivale a todos los puntos de tarjetas. No se incorporan perfil arbitral, minutos individuales, calidad de centros, entrenador actual ni estados de marcador si no se aportan fuentes nuevas.

## Puntuación de hipótesis 0–100 (prioridad, no probabilidad)

Componentes normalizados 0–1:

| Componente | Fórmula | Peso |
|---|---|---:|
| Muestra | min(min(n_propio,n_concesión)/10,1) | .20 |
| Cobertura | media de las dos coberturas | .20 |
| Estabilidad | 1/(1+max(CV)); 0 si no consta ambos | .25 |
| Concordancia | 1-abs(media_propia-media_concesión)/max(ambas,1) | .15 |
| Sensibilidad | mínimo de 1/(1+rango_leave_one_out/max(media,1)); 0 si no disponible | .10 |
| Localía | min(n_mínimo_en_sede/5,1) | .05 |
| Perfil | 1 solo con afirmación CONFIRMADO vigente, misma entidad/métrica de producción | .05 |

Se restan 10 puntos por contradicción, máximo 20: discordancia propia/concesión >40%, tendencias recientes contrarias >25% ambas, o sede vs global >40% con n_sede>=3. CV>0.8 es inestable. Para señal sustentada: n mínimo 4, cobertura>=50%, ambos CV presentes <=0.8, sin contradicción, score>=55. Son umbrales heurísticos explícitos, sin validación predictiva. n<10 registra limitación; no se exigen 30 observaciones artificialmente. Confianza MODERATE_DESCRIPTIVE exige observaciones puntuales y n>=10; en otros casos LIMITED_DESCRIPTIVE. Nunca se etiqueta confianza como porcentaje de acierto.

Las debilidades de una familia no vetan las demás. El motor devuelve cada familia ausente/inestable/contradictoria por separado. Perfiles ausentes no bloquean estadísticas. Los tres mejores scores sustentados, excluyendo xG, forman la prioridad táctica media; número de cuotas no la determina.

## Perfiles opcionales

`config/tactical_profiles/profiles.json` tiene schema_version=1.0-tactical-profiles, version y claims (vacío por defecto). No incluye hechos ilustrativos inventados. Cada claim requiere claim_id, entity_type (team/coach/player), entity_id, team_id, claim_type, source, observed_at, valid_from, valid_until, evidence_level (CONFIRMADO/PROBABLE/INFERIDO/NO_VERIFICADO), markets y texto/valor documentado.

IDs de equipo deben coincidir con el evento; una entidad team debe tener su mismo ID. Coach exige coach_ids acreditado en el fixture; player requiere verified_player_ids por equipo. coach_dependent=true exige coach_id igual al coach_ids actual: cambio lo invalida; falta de acreditación registra COACH_REGIME_UNVERIFIED. La interfaz permite estas fuentes futuras pero no fabrica entrenador actual ni minutos. NO_VERIFICADO e INFERIDO no suman el bono de confirmación. El bono exige target_metric exacto, effect_scope=PRODUCTION y claim_type específico (CORNER_GENERATION_VOLUME, SHOT_VOLUME, SHOTS_ON_TARGET_VOLUME, FOULS_COMMITTED, YELLOW_CARD_COUNTS, OFFSIDES_COMMITTED, GOALS_PRODUCTION o XG_PRODUCTION). Rutinas/eficacia de balón parado y capacidad aérea no puntúan generación de córners; perfiles de jugador no suman producción colectiva sin exposición verificable. Los perfiles se filtran por vigencia y no se convierten en conocimiento permanente. Cada rechazo queda en el JSON.

## Cruce con Bwin y scanner

Se reutiliza validate_snapshot (TXT, Event ID, SHA, cuentas, fechas, edad y margen), además de revalidar discovery y su SHA, identidad y hora, y join estadístico. Solo nombres exactos de mercados totales de tiempo reglamentario y líneas .5 verificadas se vinculan. No hay fuzzy match ni props por intuición. Mercados live, períodos, combinadas, goles de jugador y reglas ambiguas se excluyen. Amarillas solo se asocian a mercados explícitos de amarillas. Sin coincidencia se mantiene PENDIENTE_MERCADO, sin declarar que Bwin no ofrece el mercado.

Vínculos a total de partido desde una señal de equipo llevan INDIRECT_TEAM_TO_MATCH_TOTAL_NEEDS_JOINT_ANALYSIS; no equivalen a un modelo conjunto ni probabilidad. Las cuotas, nombre literal, selección, línea, hora, Event ID, fixture y fuente quedan en cada JSON. Un mercado observado pero sin modelo sigue VERIFIED_QUOTE_NO_CALIBRATED_MODEL. La captura de investigación admite la edad vigente de 120 min; revisión final mantiene última hora pre-kickoff y edad <=15 min.

## Asignación y selección

Con presupuesto estadístico explícito se reparten cupos round-robin entre bloques horarios de dos horas (antes de conocer estadísticas; no se finge calidad). Sin presupuesto no se recorta. Después del motor, Bwin/capturas se distribuyen por bloques y, dentro, prioridad táctica. Las familias sustentadas forman la puntuación y su diversidad se considera en la selección; no se afirma independencia entre tiros y tiros a puerta. Las colas completas, scores y límites se guardan en budget_assignment.json y el ledger.

Después del scanner, requisito de elegibilidad: al menos una señal sustentada con mercado real, scanner VERIFIED, preflight válido, fixture futuro y margen >=45. La cola conjunta actual (0–100, NO valor) usa `.60 * tactical_research_score + .40 * market_execution_readiness`, resta hasta 15 puntos por contradicciones y aplica, cuando existe, un ajuste exploratorio acotado derivado del scanner (máximo +15; -10 cuando todas las líneas evaluadas son negativas bajo sus escenarios). `market_execution_readiness` conserva sus componentes explícitos de cobertura por familias, diversidad, identidad, margen y scanner. Para nivel B se sustituye la prioridad táctica por la media descriptiva de hasta tres señales investigables. No es probabilidad, EV ni calibración y permite no seleccionar a nadie. La comparación y componentes de todos los candidatos figuran en TACTICAL_SELECTION_REPORT.md y el handoff.

CSV: columnas tácticas compactas, ruta a tactical/fixtures/<fixture_id>.json y modo de evaluación. No se incrustan perfiles, señales ni mercados enormes en celdas. Las filas NO_EVALUADO se conservan. La copia histórica usa otra carpeta; no se reemplazan fuentes originales ni capturas. El informe del motor es una preparación técnica, no Codex analista final.
