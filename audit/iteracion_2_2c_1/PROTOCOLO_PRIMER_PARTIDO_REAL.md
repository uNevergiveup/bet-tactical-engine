# Protocolo Operativo — Primer Partido Real PREMATCH (Iteración 2.2C.1)

## 1. Registrar predicción congelada antes del kickoff
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py record \
  --match-engine outputs/matches/<EVENT_ID>/inputs/match_engine_<EVENT_ID>.json \
  --bwin-txt outputs/matches/<EVENT_ID>/inputs/cuotas_bwin_<EVENT_ID>.txt \
  --shadow-json outputs/matches/<EVENT_ID>/shadow/shadow_<EVENT_ID>.json \
  --manifest audit/iteracion_2_2b/MANIFEST_MODELOS_CONGELADOS.json \
  --corpus-root . \
  --records-dir outputs/prospective_records
```
Comprobar en la salida JSON:
- `temporal_integrity_status == "PREMATCH_VERIFIED"`
- `fixture_identity_verified == true`
- `model_comparison_integrity.full_four_model_comparison_valid == true`
- `odds_status == "ODDS_VERIFIED"` (si se adjuntó `--bwin-txt` con cabecera `Captura:` válida)

## 2. Fijar huella del registro inmutable
```bash
shasum -a 256 outputs/prospective_records/<FIXTURE_ID>_<EVENT_ID>.json
```

## 3. Liquidar resultado oficial tras el partido (en archivo separado)
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py settle \
  --record outputs/prospective_records/<FIXTURE_ID>_<EVENT_ID>.json \
  --hg <HG> --ag <AG> --status FT \
  --source-json outputs/matches/<EVENT_ID>/inputs/target_fixture_<FIXTURE_ID>.json
```
- El archivo original `outputs/prospective_records/<FIXTURE_ID>_<EVENT_ID>.json` permanece intacto byte a byte.
- La liquidación se guarda en `outputs/prospective_records/settlements/<FIXTURE_ID>_<EVENT_ID>.settlement.json`.

## 4. Evaluar comparativa pareada (independiente de NO BET)
```bash
PYTHONPATH=. .venv/bin/python tools/prospective_tracker.py evaluate \
  --records-dir outputs/prospective_records
```
