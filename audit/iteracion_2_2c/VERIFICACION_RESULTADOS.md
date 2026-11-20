# Verificación de Resultados Deportivos y Separación de Métricas (Iteración 2.2C)

## 1. Diseño de verificación (`verify_authoritative_result`)
Al ejecutar `settle_prospective_record` con `--source-json` (`authoritative_fixture`), el sistema verifica contra el bloque inmutable `pre_kickoff`:
1. `fixture_id` coincidente con `pre_kickoff["fixture_id"]`.
2. `home_id` y `away_id` coincidentes con `pre_kickoff["teams"]`.
3. `league_id` coincidente con `pre_kickoff["competition"]["league_id"]` (si consta en la fuente).
4. Estado final `status.short` en `{"FT", "AET", "PEN"}` y coincidente con `--status`.
5. Marcador `goals.home` y `goals.away` no nulo y coincidente con `--hg` y `--ag`.

Si cualquier campo discrepa, la liquidación se rechaza con `SETTLEMENT_VERIFICATION_MISMATCH`.

## 2. Clasificación de resultados y exclusión de métricas acreditables

| Modo / Origen | `verification_status` | `creditable` | ¿Entra en `evaluate` (`settled_paired_n`, `ALL`, `CLUB`, `NATIONAL_TEAM`)? |
| --- | --- | --- | --- |
| `--source-json` válido y verificado | `VERIFIED_AUTHORITATIVE` | `true` | **Sí** (si `eligible_for_paired_eval == true`) |
| Entrada manual sin fuente autorizada | `UNVERIFIED_RESULT` | `false` | **No** (contabilizado aparte en `unverified_result_n`) |
| `--simulate` (tests/desarrollo) | `SIMULATED_TEST_ONLY` | `false` | **No** (contabilizado aparte en `simulated_test_n`) |

## 3. Independencia respecto a `NO BET` de Shadow
- `evaluate_prospective_records` mide la calidad predictiva (`RPS`, `Log-Loss`, `Brier`, `MAE`, `bias`, sesgo de favoritos `>= 0.55`) sobre todos los registros `VERIFIED_AUTHORITATIVE` elegibles, **con independencia de que Shadow haya emitido `NO BET`**.
- Referencia histórica de producción (Iteración 2.2B, **no mezclada** con registros prospectivos futuros):
  - `159` directorios en `outputs/matches/`:
    - `149` expedientes incompletos (`93.71%`).
    - `4` expedientes completos sin JSON de Shadow.
    - `1` bloqueo operativo/temporal PREMATCH (`7909713`).
    - `5` decisiones evaluables de Shadow terminadas en **NO BET estadístico** (`7905477`, `7905487`, `7909704`, `7909714`, `7912816`).
  - Esos `5` eventos históricos anteriores a `CORPUS_CUTOFF_UTC` quedan estrictamente fuera del contador prospectivo.
