# Objetivo 5 — cobertura y descubrimiento Bwin

## Estado

Auditoría cerrada sobre los artefactos congelados y un único piloto dirigido autorizado de 10 fixtures. No se consultó API-Football, no se ejecutó ningún LLM y no se capturaron cuotas. Los snapshots originales permanecen intactos.

El resultado mejora la identidad, pero **no justifica declarar cobertura completa ni READY comercial**: 42 de 73 fixtures siguen sin evento compatible observado en las páginas consultadas; 13 tienen candidato pero carecen de evidencia suficiente de categoría; dos carecen de hora absoluta y uno contradice el horario API.

## Diagnóstico demostrado

- El catálogo de 168 eventos era parcial. Las páginas generales congeladas devolvían 50 eventos para `hoy`, 50 para `mañana` y 13 para la raíz; repetir 30 desplazamientos no cambió esos conjuntos.
- La navegación pública expuso 52 enlaces contextuales reales. El piloto visitó cuatro páginas Bwin-first (África, Alemania, América y Arabia Saudí) y una página dirigida de España. Produjeron 25, 36, 12, 0 y 29 eventos respectivamente.
- El catálogo combinado y deduplicado contiene 266 Event IDs. Ningún Event ID se construye ni se adivina: todos proceden de enlaces DOM observados o de snapshots locales previos.
- La geometría del piloto (`scrollY=0`, viewport 900, documento 905) demuestra que el desplazamiento de ventana no recorría una lista interna. Se amplió el colector para localizar cualquier contenedor visible con `overflow-y: auto|scroll`, desplazarlo y registrar su geometría. Si no existe superficie desplazable y no aparecen eventos nuevos durante cinco muestras, termina sin repetir 30 ciclos inútiles.
- Se corrigieron falsos negativos de normalización multilingüe, nombres institucionales femeninos y títulos de selecciones nacionales. La identidad sigue exigiendo equipos en orden, hora, competición y categoría compatibles.
- La categoría sénior puede corroborarse mediante competición exacta (`score >= 0.95`), fixture adulto/elegible y ausencia de señales femenina/juvenil/reserva. Esto no convierte una similitud nominal en identidad.
- La reconciliación admite varios descubrimientos congelados, deduplica por Event ID y conserva la mejor inspección por fixture. Así, una corrida parcial no borra evidencia verificada anterior.

## Piloto dirigido

- Fixtures: 10.
- Páginas contextuales: 5 de un máximo autorizado de 10 (4 Bwin-first + 1 API-first contextual).
- Duración observada de navegación: 164.226 s.
- Solicitudes de recursos observadas por el navegador: 7.288. Este número incluye recursos internos, telemetría y reintentos del sitio; no equivale a páginas ni a adquisiciones de cuotas.
- Errores de extracción relevantes: Arabia Saudí quedó `EMPTY_OR_BLOCKED`; no hubo error fatal. Tres contextos pedidos (Copa de la Reina, UEFA Nations League y Africa Cup of Nations Qualification) no tenían enlace resoluble en la navegación observada.
- Recuperaciones demostradas en el piloto: Deportivo Pasto–Junior (`7912964`) y SL Benfica W–Austria Wien W (`7910156`). Armenia–Cyprus (`7909685`) y Türkiye–Belgium (`7905470`) quedaron inicialmente en revisión por un defecto de comparación del título multilingüe, corregido y verificado offline después.

## Antes y después (mismos 73 fixtures)

| Métrica | Antes | Después |
|---|---:|---:|
| Eventos de catálogo deduplicados | 168 | 266 |
| Fixtures con candidato | 22 | 31 |
| Identidades verificadas | 9 | 15 |
| Sin candidato observado | 51 | 42 |
| Capturas de cuotas verificadas | 4 | 4 |
| Mercado comercial compatible verificado | 0 | 0 |

El aumento de identidades no implica captura de cuotas ni compatibilidad comercial. Las cuatro capturas previas se preservan y no se amplían en esta fase.

## Bloqueos actuales

- `NOT_OBSERVED_IN_LOCAL_CATALOG`: 42. Significa ausencia en los catálogos consultados, no inexistencia en Bwin. Por país: World 21, Argentina 6, Brazil 4, Liberia 3, Uruguay 3, Congo-DR 2 y uno en Spain, Jamaica y Mexico.
- `CANDIDATE_CATEGORY_UNKNOWN`: 13. El nombre/hora puede ser compatible, pero falta corroboración fiable de categoría o competición; no se verifica la identidad.
- `CANDIDATE_TIME_UNVERIFIED`: 2. Falta reloj absoluto o cabecera prepartido acotada.
- `CANDIDATE_TIME_CONFLICT`: 1. El horario observado contradice el fixture API y permanece bloqueado.
- Los grupos con más ausencias son Africa Cup of Nations Qualification y Reserve League (5 cada uno), seguidos por Friendlies, LFA First Division, CONCACAF Nations League y Segunda División (3 cada uno). El detalle por fixture está en `reconciliation_73.json` y `reconciliation_73.csv`.

## Integración y reanudación

`prematch_execution_control.py` incorpora presupuestos separados para páginas Bwin-first, páginas contextuales y desplazamientos. El plan queda sellado por hash, el controlador reserva el presupuesto antes de ejecutar y `--resume` no repite una etapa Bwin ya completada. Un descubrimiento local previo puede pasarse como `--bwin-input`; se reutiliza como snapshot de catálogo sin convertir sus horarios en evidencia vigente.

## Artefactos

- Piloto inmutable: `outputs/phase1/2026-11-12_1500_0300/objective5_pilot_01/bwin_discovery.json`.
- Reconciliación final: `outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_v3/reconciliation_73.json`.
- Matriz CSV: `outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_v3/reconciliation_73.csv`.
- Resumen: `outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_v3/summary.json`.

## Comandos reproducibles

Reconciliación offline (admite repetir `--discovery`):

```bash
.venv/bin/python -B bwin_offline_reconcile.py \
  --fixtures outputs/phase1/2026-11-12_1500_0300/fixtures_discovered.json \
  --discovery outputs/phase1/2026-11-12_1500_0300/browser_retry_02/bwin_scan/bwin_discovery.json \
  --discovery outputs/phase1/2026-11-12_1500_0300/objective5_pilot_01/bwin_discovery.json \
  --output-dir outputs/control/runs/python_engine_real_v1/bwin_reconciliation_objective5_next
```

Pruebas offline:

```bash
.venv/bin/python -m unittest -v \
  test_bwin_offline_reconcile.py \
  test_prematch_phase1.py \
  test_prematch_execution_control.py
```

Piloto futuro limitado (requiere aprobación externa explícita y no debe usarse para cuotas):

```bash
.venv/bin/python -B bwin_discovery.py \
  --input outputs/phase1/2026-11-12_1500_0300/fixtures_discovered.json \
  --fixture-id FIXTURE_ID --directed-pilot --bwin-first-max-pages 4 \
  --max-country-pages 6 --max-scrolls 12 --headless --confirm-external \
  --output-dir outputs/bwin_discovery/NUEVO_RESULTADO_INMUTABLE
```

