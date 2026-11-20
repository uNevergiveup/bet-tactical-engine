# Diseño del Manifiesto de Acreditación (No Activado) — Iteración 2.2B

**Estado actual en producción:** **`calibration_accredited_families = frozenset()`** (sin activar hasta superar evaluación prospectiva y recalibración de intervalos).

---

## 1. Estructura versionada del manifiesto por familia y estrato

```json
{
  "schema_version": "1.0-accreditation",
  "model_version": "CoherentMatchModeler_baseline_2.0_2.1A",
  "model_code_sha256": "<sha256>",
  "validation_cutoff_utc": "2026-11-14T19:45:00+00:00",
  "strata": {
    "CLUB": {
      "oos_n": 200,
      "families": {
        "MATCH_RESULT_1X2": {"accredited": false, "rps": null, "log_loss": null, "ece": null, "reason": "PENDING_PROSPECTIVE_VALIDATION"},
        "GOALS_FT": {"accredited": false, "pit_uniformity_ok": null, "reason": "PENDING_PROSPECTIVE_VALIDATION"},
        "BTTS": {"accredited": false, "reason": "PENDING_PROSPECTIVE_VALIDATION"},
        "CORNERS": {"accredited": false, "reason": "INSUFFICIENT_JOINT_DETAILED_SAMPLE"},
        "YELLOW_CARDS": {"accredited": false, "reason": "INSUFFICIENT_JOINT_DETAILED_SAMPLE"}
      }
    },
    "NATIONAL_TEAM": {
      "oos_n": 0,
      "families": {
        "MATCH_RESULT_1X2": {"accredited": false, "reason": "INSUFFICIENT_STRATUM_SAMPLE_LT_50"}
      }
    }
  }
}
```

## 2. Reglas de consumo seguro en `ShadowPipelineRunner` (futura Iteración 2.3)

1. El manifiesto solo se aplica si `manifest["model_version"] == model.model_version` y `manifest["model_code_sha256"]` coincide con el código en ejecución; en caso contrario, degrada automáticamente a `frozenset()`.
2. La acreditación es **independiente por `(stratum, market_family)`**: acreditar `MATCH_RESULT_1X2` en `CLUB` **no** acredita `NATIONAL_TEAM` ni acredita `CORNERS`/`SHOTS`/`FOULS`/`YELLOW_CARDS`/`GOALS_H1`.
3. En Iteración 2.2B **no** se activa ninguna familia ni se modifican `p_low`/`p_high`.
