# Protocolo Operativo — Primera Prueba PREMATCH Real (Iteración 2.2C)

## 1. Requisitos previos
- Expediente del partido futuro generado y validado **antes del kickoff** (`match_engine_<EVENT_ID>.json` con `status = NS` y `kickoff_utc > now_utc`).
- Manifiesto congelado: `audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json` (`M0`, `M0_plus_Elo`, `M4`, `M4_plus_Elo`).
- No alterar `CoherentMatchModeler`, umbrales PREMATCH ni el dictamen normal de Shadow.

## 2. Paso 1 — Registrar predicción congelada antes del kickoff
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py record \
  --match-engine outputs/matches/<EVENT_ID>/inputs/match_engine_<EVENT_ID>.json \
  --bwin-txt outputs/matches/<EVENT_ID>/inputs/cuotas_bwin_<EVENT_ID>.txt \
  --shadow-json outputs/matches/<EVENT_ID>/shadow/shadow_<EVENT_ID>.json \
  --manifest audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json \
  --records-dir outputs/prospective_records
```
El comando imprime:
- `record_path` (`outputs/prospective_records/<FIXTURE_ID>_<EVENT_ID>.json`)
- `fixture_id`, `event_id`
- `kickoff_utc` y `kickoff_madrid`
- `temporal_integrity_status` (`PREMATCH_VERIFIED`)
- `history_summary` (`home_raw_n`, `away_raw_n`, `home_n_eff_m0`, `away_n_eff_m0`)
- `pre_kickoff_sha256`

## 3. Paso 2 — Confirmar inmutabilidad del registro
Guardar el hash del archivo inmediatamente después del registro:
```bash
shasum -a 256 outputs/prospective_records/<FIXTURE_ID>_<EVENT_ID>.json
```
Este archivo **no vuelve a escribirse** en ninguna fase posterior.

## 4. Paso 3 — Liquidar resultado verificado después del partido (`FT` / `AET` / `PEN`)
Una vez finalizado el encuentro y obtenido el JSON oficial post-partido (`<TARGET_FIXTURE_JSON>`):
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py settle \
  --record outputs/prospective_records/<FIXTURE_ID>_<EVENT_ID>.json \
  --hg <HG> --ag <AG> --status FT \
  --source-json <TARGET_FIXTURE_JSON>
```
- Escribe únicamente en `outputs/prospective_records/settlements/<FIXTURE_ID>_<EVENT_ID>.settlement.json`.
- Clasifica como `VERIFIED_AUTHORITATIVE` (`creditable=true`) solo si `fixture_id`, equipos, liga, estado y marcador coinciden con `--source-json`.
- Si se omite `--source-json`, queda marcado como `UNVERIFIED_RESULT` y **no entra en métricas acreditables**.

## 5. Paso 4 — Evaluar comparativa pareada (independiente de NO BET)
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py evaluate \
  --records-dir outputs/prospective_records
```
Verifica `shasum -a 256` del archivo original tras `settle` para confirmar que no cambió ni un byte.
