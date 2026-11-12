# Auditoría focalizada — snapshot 9 noviembre

Se inspeccionaron el código, observaciones y 248 asociaciones de
`outputs/advanced_discovery_v2/phase3_release/20261109/`.

## CROSSED y uso predictivo

Los 58 CROSSED relacionan goles propios y concedidos del mismo histórico. Son
asociaciones retrospectivas; la condición no se conoce antes de jugar. El contrato
ya marcaba esa condición en applicability como POSTMATCH_ASSOCIATION_NOT_A_PREMATCH_TRIGGER,
pero faltaba un bloqueo independiente en la raíz del patrón y del expediente.
Se añadió predictive_use.authorized=false a todos los patrones, disponibilidad
prepartido de la variable y razón SAME_MATCH_POST_KICKOFF_VARIABLE cuando corresponde.
Tener una variable disponible tampoco autoriza predicción sin modelo validado.
No se sustituye conceded.goals por una estimación inventada. Para una futura
característica prepartido habrá que construir concesión previa del rival, con IDs
y fechas anteriores, y validar otra hipótesis fuera de muestra.

## Los 1587 rechazos individuales

Se consultó el índice completo de las 39 respuestas locales de fixtures: ninguno
de los 1587 históricos equipo/fixture del snapshot tiene una identidad en esa caché.
Por tanto el motivo comprobado es FIXTURE_CACHE_ABSENT, no contradicción de equipos,
error de alias ni indisponibilidad demostrada del proveedor. El normalizador previo
agrupa este caso bajo FIXTURE_IDENTITY_UNAVAILABLE: su gate es correcto, pero el
mensaje era menos específico que la evidencia de adquisición.

Existen 28 archivos fixtures_players y 228 fixtures_statistics en la caché global;
su existencia no acredita datos para estos históricos. El informe JSON conserva
por cada histórico si hay players/statistics huérfanos de identidad y por grupo
competición/temporada/género/equipo la cobertura de cada familia. No se inventan
filas por jugador: players=[] significa identidad no adquirida; no cero jugadores
en la competición. La disponibilidad comercial Bwin queda NOT_AUDITED: es una
dimensión independiente. Sin consultas online no puede acreditarse el plan API ni
la cobertura que devolvería para esos fixtures.

## Por qué solo own.goals

Los 248 resultados son own.goals. Se reprodujo split_history por equipo,
competición y temporada: en los 230 segmentos, remates, córners y tiros a puerta
tienen menos de seis valores válidos en entrenamiento. El generador exige dos
grupos de tres, además de variación y condición no vacía. Los detalles fueron
adquiridos principalmente para partidos recientes y quedan concentrados en holdout.
No es ausencia total de estadísticas: remates 157/1587, córners y tiros a puerta
161/1587. No se detectó un filtro que excluya esas métricas; los tests sintéticos
descubren resultados nuevos no programados. No se redujeron los umbrales ni se
trasladaron datos de holdout al entrenamiento para fabricar resultados.

## Expedientes y fechas

mechanism=null no significa explicación completada. El patrón ya era
PENDING_EXPLANATION_WITH_SOURCES; el expediente ahora declara además
explanation_status=PENDING_EXTERNAL_EVIDENCE y copia el bloqueo predictivo.
generated_at_utc del snapshot es el corte congelado, no fecha de adquisición
independiente de cada endpoint. Las referencias de caché usan fetched_at_utc y SHA;
el corte efectivo es el menor corte original. Fechas finales estimadas con +3h son
una política conservadora, no un silbatazo oficial acreditado. La opción de caché
retrospectiva debe etiquetarse y no acredita disponibilidad contemporánea.

## Recuperación priorizada, sin ejecutarla

1. Elegir equipos con al menos seis resultados colectivos comparables y familias
   con demanda estadística. Recuperar primero identidad de históricos faltantes
   mediante las interfaces /fixtures ya implementadas, deduplicando fixture_id.
   Reconciliar equipos, sede, liga, temporada, fecha y estado final; reutilizar
   cualquier detalle válido existente antes de nuevas peticiones.
2. Priorizar estadísticas antiguas que completen entrenamiento de remates/córners;
   adquirir solo faltantes. No seguir acumulando únicamente el tramo reciente.
3. Para series individuales, obtener players y lineups de esos mismos fixtures,
   por etapas con presupuesto y cobertura auditada. Empezar por una muestra piloto,
   no por todos los jugadores de toda la jornada. Verificar minutos y titularidades.
4. Si el proveedor devuelve vacío/no cobertura, conservar pendiente y registrar
   estado; no reparar con números imputados. Los endpoints existentes no prueban
   su disponibilidad para cada liga ni la contratación actual.
5. Eventos y períodos solo después de asegurar identidad y muestra básica.
   Verificación Bwin se realiza independientemente cuando exista candidato comercial.

Informe reproducible:

```bash
.venv/bin/python prematch_v2_coverage_audit.py \
 --base outputs/advanced_discovery_v2/phase3_release/20261109 \
 --cache-root outputs/.api_football_history_cache \
 --output outputs/advanced_discovery_v2/audit_20261109/coverage_audit.json
.venv/bin/python -m unittest -q test_prematch_advanced_discovery_v2.py
```

El informe es una salida separada; los snapshots históricos no se modifican.
11 tests PASS con regresión explícita del bloqueo predictivo. No se realizaron
consultas API, scraping ni cambios de significación. La LLM táctica sigue pendiente.
