# Objetivo 4: recuperación offline de Event IDs Bwin

Estado: auditoría offline completada; piloto online no ejecutado. No se llamó
API-Football, no se abrió Bwin, no se ejecutó scraper y no se modificaron
modelos comerciales, ranking ni reglas de apuestas.

## Resultado sobre el catálogo congelado

Entrada: 73 fixtures API y 168 enlaces de eventos Bwin observados el
12/11/2026. El catálogo sólo registra tres páginas genéricas (`hoy`, `mañana`
y fútbol general), todas con estado OK. No registra páginas nacionales:
`country_discovery.requested_countries=[]` y `country_urls_found=0`.
Por tanto, ausencia en este catálogo no demuestra ausencia en Bwin.

| Estado posterior | Fixtures |
|---|---:|
| Identidad verificada offline | 6 |
| Candidato con categoría desconocida | 7 |
| Candidato sin hora verificable | 2 |
| Candidato con conflicto horario | 1 |
| Sin candidato compatible en los 168 eventos | 57 |
| Total | 73 |

Hay 16 fixtures con Event ID candidato, seis identidades verificadas, cuatro
capturas de cuotas con SHA verificado y cero fixtures con un mercado aceptado
por el modelo comercial congelado. Son hitos distintos.

La causa de los 69 fixtures sin captura queda cerrada a este nivel de evidencia:
57 no tienen un evento de ambos equipos compatible en las fuentes locales y 12
sí tienen candidato, pero carecen de captura local. Entre esos 12 hay siete
categorías desconocidas, dos horas no verificadas, un conflicto horario y dos
identidades recuperadas offline sin captura. No se afirma que los 57 eventos
no existieran en Bwin.

La ejecución antigua comparó sólo los 54 registros `eligible`; los 19 registros
`excluded` no entraron en el reconciliador. Ahora los 73 recorren la misma fase.
De los 19 omitidos, 18 siguen sin candidato local y Barracas Central Res.–
Argentinos Juniors Res. recupera el candidato 7910506, pendiente de hora. Los
conflictos de género, reservas y categorías juveniles continúan bloqueados.

## Casos exigidos

- **1644370 Calvo Sotelo–Albacete / 7910678.** Nombres, orden, Copa del Rey y
  hora 18:30 coinciden. `UNKNOWN` se conserva como ausencia de evidencia:
  `CANDIDATE_CATEGORY_UNKNOWN`. Ya no equivale a masculino ni a contradicción.
- **1520978 São Bernardo–Athletic Club / 7911881.** La cabecera PREMATCH
  conservada dice `Mañana / 0:00`, capturada el 12/11 a las 03:45 Madrid. El
  parser rechazaba `0:00` por exigir dos dígitos en la hora. Corregido: resuelve
  13/11 00:00 Europe/Madrid. Continúa pendiente por categoría desconocida.
- **1549870 Llaneros–Chico / 7912958.** La misma corrección interpreta
  `Mañana / 1:00` desde el instante de captura, no desde el reloj actual.
  Coinciden hora, categoría masculina y competición: identidad recuperada.
- **1528963 Albania–Finland / 7909679.** `Finland` no generaba el candidato
  español `Finlandia`. Se añadió una traducción exacta de selección nacional
  que sólo genera candidatos. El Event ID se recupera; falta hora y competición
  verificables, de modo que la identidad sigue pendiente.
- **1649147 Real Potosí–Nacional Potosí / 7910426.** La cabecera retenida indica
  13/11 00:00; API indicaba 12/11 23:00. Se conserva el conflicto de 60 minutos
  y se exige revisión de programación. No se acepta la asociación.

También pasa a identidad verificable Cerro Largo–Progreso (7910405) mediante
la cabecera congelada. Las cuatro identidades ya verificadas permanecen:
Barcelona W–Arsenal W, Águila–Municipal Limeño, Atlético Balboa–Inca Aruba y
Aurora–Mixco. La matriz conserva evidencia y causa individual por fixture.

## Correcciones

1. `read_candidates` ya no excluye silenciosamente `category_status=EXCLUDED`:
   todos los fixtures del inventario generan candidatos; las incompatibilidades
   se aplican después como gates de identidad.
2. Normalización candidata para `Finland`/`Finlandia` y variantes explícitas de
   Türkiye. No certifica identidades ni crea URLs.
3. Cabecera PREMATCH acepta hora de una o dos cifras, mantiene minuto de dos,
   zona Europe/Madrid, captura máxima de 120 segundos, fecha estable y rechazo
   de pliegues DST ambiguos.
4. `Hoy`/`Mañana` se ancla al timestamp observado del propio evento. Nunca usa
   `datetime.now()` para reinterpretar snapshots.
5. Nuevo reconciliador offline completo y modo `--fixture-id` dirigido.
6. El descubridor online incorpora `--directed-pilot`: exige entre uno y diez
   Fixture IDs y limita las páginas nacionales a diez. La confirmación externa
   existente sigue siendo obligatoria.

Las reglas de normalización sólo producen candidatos. Orden invertido, LIVE,
masculino/femenino, reservas/primer equipo, juveniles, duplicados y conflicto
horario siguen sin poder certificar una identidad.

## Artefactos

La evidencia final está en
`outputs/control/runs/python_engine_real_v1/bwin_reconciliation_v5/`:

- `reconciliation_73.json`: matriz completa, candidatos, atributos, páginas,
  horas, categoría, competición, causa y recuperación.
- `reconciliation_73.csv`: vista plana con los campos solicitados.
- `summary.json`: conteos separados.

Cada entrada vincula los SHA256 del inventario, descubrimiento, cuatro
preflights y mapeo comercial utilizado. Los originales no se sobrescribieron.

## Comandos reproducibles

Auditoría completa local (usar una carpeta destino nueva):

```sh
.venv/bin/python -B bwin_offline_reconcile.py \
  --fixtures outputs/phase1/2026-11-12_1500_0300/fixtures_discovered.json \
  --discovery outputs/phase1/2026-11-12_1500_0300/browser_retry_02/bwin_scan/bwin_discovery.json \
  --preflight outputs/control/runs/python_engine_real_v1/local_preflight/1638295/market_preflight.json \
  --preflight outputs/control/runs/python_engine_real_v1/local_preflight/1600658/market_preflight.json \
  --preflight outputs/control/runs/python_engine_real_v1/local_preflight/1600659/market_preflight.json \
  --preflight outputs/control/runs/python_engine_real_v1/local_preflight/1559703/market_preflight.json \
  --market-mapping outputs/control/runs/python_engine_real_v1/commercial_v2/mapping_after/market_mapping.json \
  --output-dir outputs/control/runs/python_engine_real_v1/bwin_reconciliation_manual_new
```

Para un solo fixture añadir `--fixture-id 1528963`. Esto sigue siendo offline.

Un futuro piloto online, sólo tras autorización explícita y mientras los
fixtures sean PREMATCH, se prepara así; el hash/confirmación debe proceder del
flujo manual autorizado y no se ejecutó en esta entrega:

```sh
.venv/bin/python -B bwin_discovery.py \
  --input RUTA_INVENTARIO_ACTUAL \
  --fixture-id ID1 --fixture-id ID2 \
  --directed-pilot --max-country-pages 10 --max-scrolls 12 \
  --inspection-retries 1 \
  --headless --confirm-external \
  --output-dir RUTA_NUEVA
```

La captura de mercados es una autorización y etapa posterior, sólo para una
identidad verificada. El piloto conserva snapshots nuevos en otra carpeta y
debe detenerse ante errores de acceso; no hay evasión ni reintentos ilimitados.

## Estado de preparación

Verificación final: **687 pruebas**, cero fallos, cero errores y diez omisiones
documentadas. La suite bloqueó red y escrituras en los outputs originales;
`git diff --check` terminó limpio. Las regresiones cubren los cinco casos
reales, categorías desconocidas, femenino/masculino, reservas, orden invertido,
duplicados, mismos equipos, LIVE, catálogo parcial, ausencia sin inexistencia,
fixtures fuera del catálogo legado, medianoche y modo por Fixture ID.

El reconciliador está preparado para futuros inventarios completos y búsqueda
dirigida. Esta auditoría no declara 73/73 ni READY comercial. Quedan 57 eventos
no observados en las fuentes locales, nueve identidades candidatas pendientes
por categoría u hora y un conflicto horario. Resolverlos exige un catálogo
actual y, cuando proceda, inspección del evento real antes del kickoff.
