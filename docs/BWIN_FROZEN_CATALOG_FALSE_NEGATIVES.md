# Auditoría adicional del catálogo Bwin congelado

Alcance: reconciliación completamente offline de los mismos 73 fixtures contra
los mismos 168 eventos. No se consultó Bwin, API-Football ni otra fuente.

## Antes y después

| Medida | Reconciliación v5 | Reconciliación v7 |
|---|---:|---:|
| Fixtures con candidato Event ID | 16 | 21 |
| Identidades verificadas offline | 6 | 6 |
| Capturas de cuotas verificadas | 4 | 4 |
| Sin candidato observado | 57 | 52 |
| Falsos negativos de generación corregidos | 0 | 5 |

La ausencia de nuevas identidades verificadas es deliberada. Los cuatro
partidos internacionales tienen hora compatible pero categoría `UNKNOWN` y
competición no corroborada por el evento congelado. Benfica–Austria acredita
género femenino y hora, pero carece de evidencia de competición en el catálogo.

## Causa y resultado individual

| Fixture | Event ID | Defecto | Resultado posterior |
|---|---:|---|---|
| Türkiye–Belgium | 7905470 | Belgium no reconocía Bélgica | CANDIDATE_CATEGORY_UNKNOWN |
| Italy–France | 7905471 | Italy/France no reconocían Italia/Francia | CANDIDATE_CATEGORY_UNKNOWN |
| Czechia–Spain | 7905479 | Czechia/Spain no reconocían Chequia/España | CANDIDATE_CATEGORY_UNKNOWN |
| England–Croatia | 7905480 | England/Croatia no reconocían Inglaterra/Croacia | CANDIDATE_CATEGORY_UNKNOWN |
| SL Benfica W–Austria Wien W | 7910156 | Prefijos SL/FK, Wien/Viena y W/F | CANDIDATE_COMPETITION_UNVERIFIED |

Los cinco Event IDs, URLs, textos y timestamps proceden de
`browser_retry_02/bwin_scan/bwin_discovery.json`. En todos los casos el reloj
relativo `Hoy` se resolvió desde `text_observed_at_utc`; las diferencias con API
son cero minutos. Esto sólo resuelve el atributo temporal.

## Corrección conservadora

Las traducciones de selecciones se aplican únicamente en contexto internacional
(`country=World/International`) y nunca si los nombres contienen señales
juveniles. No se agregaron aliases globales difusos. Los aliases de Benfica y
Austria son exactos, conservan los marcadores femeninos y sólo generan
candidatos. Un equipo masculino o U19 no los hereda.

La puntuación de nombre continúa sin certificar identidad. Siguen vigentes:

- orden local/visitante;
- exclusión LIVE;
- conflicto masculino/femenino;
- reservas y juveniles;
- hora capturada y zona Europe/Madrid;
- competición corroborada;
- bloqueo por duplicados o múltiples partidos.

## Artefactos y reproducción

La matriz nueva está en
`outputs/control/runs/python_engine_real_v1/bwin_reconciliation_v7/`.
`reconciliation_73.json` conserva la evidencia anidada y
`reconciliation_73.csv` la vista plana. Las entradas originales permanecen
intactas.

Para reproducir, usar el mismo comando del informe del Objetivo 4 y elegir una
carpeta destino nueva. Para revisar sólo un caso se mantiene `--fixture-id`.
No se ejecutó el piloto online.
