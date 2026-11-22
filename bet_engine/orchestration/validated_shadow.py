"""Validated finish integration around the frozen Shadow adapter."""

import hashlib
import json
from pathlib import Path

from .adapters import run_shadow_for_event_dir


def run_validated_shadow(root, event_id, **kwargs):
    ev_dir = Path(root) / "outputs" / "matches" / str(event_id)
    try:
        manifest = json.loads((ev_dir / "manifest.json").read_bytes())
        inputs = manifest.get("inputs") or {}
        validation_ref = manifest.get("validation_artifact") or {}
        validation_path = ev_dir / "validation" / f"validacion_{event_id}.json"
        if (str(manifest.get("event_id")) != str(event_id)
                or manifest.get("status") not in {"FINALIZADO", "AUDITORIA_HISTORICA"}
                or not (manifest.get("validation_summary") or {}).get("motor_allowed")
                or Path(validation_ref.get("path", "")).resolve() != validation_path.resolve()):
            raise ValueError("SHADOW_VALIDATION_BINDING_MISSING")
        validation_bytes = validation_path.read_bytes()
        validation = json.loads(validation_bytes)
        if (hashlib.sha256(validation_bytes).hexdigest() != validation_ref.get("sha256")
                or validation.get("status") == "BLOQUEADO_PREPARTIDO"
                or any(item.get("severity") == "BLOCK" for item in validation.get("issues", []))):
            raise ValueError("SHADOW_VALIDATION_REJECTED")
        for key in ("context_json", "context", "identity", "report", "odds", "manual_lineups"):
            ref = inputs.get(key)
            if not ref and key in {"odds", "manual_lineups"}:
                continue
            if not ref or hashlib.sha256(Path(ref["path"]).read_bytes()).hexdigest() != ref.get("sha256"):
                raise ValueError(f"SHADOW_INPUT_SHA_MISMATCH:{key}")

        alternative = inputs.get("qualitative_research") or {}
        if alternative.get("path") and (
            Path(alternative["path"]).resolve() != Path(inputs["report"]["path"]).resolve()
        ):
            raise ValueError("SHADOW_ALTERNATIVE_REPORT_NOT_VALIDATED")
        context = json.loads(Path(inputs["context_json"]["path"]).read_bytes())
        identity = json.loads(Path(inputs["identity"]["path"]).read_bytes())
        if (str((context.get("match") or {}).get("event_id")) != str(event_id)
                or str(identity.get("api_fixture_id")) != str((context.get("fixture") or {}).get("fixture_id"))):
            raise ValueError("SHADOW_VALIDATED_FIXTURE_MISMATCH")
        result = run_shadow_for_event_dir(root, event_id, **kwargs)
        if not result.get("ok"):
            return result
        path = Path(result["path"])
        summary = json.loads(path.read_bytes())
        report = summary["shadow_report"]
        if (hashlib.sha256(validation_path.read_bytes()).hexdigest() != validation_ref["sha256"]
                or summary.get("context_json_sha256") != inputs["context_json"]["sha256"]
                or summary.get("odds_sha256") != (inputs.get("odds") or {}).get("sha256")):
            raise ValueError("SHADOW_VALIDATED_INPUT_CHANGED")
        odds_freshness_status = summary["odds_freshness_status"]
        summary.update({
            "validation_artifact": validation_ref,
            "actionable_bet": False,
            "selection_evaluations": [
                {
                    "selection_key": row["selection_key"],
                    "market_family": row["market_family"],
                    "contract_id": row.get("canonical_contract_id"),
                    "estimated_probability": row.get("p_model"),
                    "calibration_status": row.get("calibration_status"),
                    "uncertainty": {key: row.get(key) for key in ("p_low", "p_high", "raw_sample_n", "effective_sample_n")},
                    "settlement": {key: row.get(key) for key in ("settlement_rule", "p_win_unconditional", "p_push_refund")},
                    "price": {
                        "snapshot_status": odds_freshness_status,
                        **{key: row.get(key) for key in ("odd", "break_even_prob", "fair_odd", "ev_model", "ev_conservative_low")},
                        "assessment": (
                            "NO_MODEL" if row.get("p_model") is None else
                            "NO_USABLE_QUOTE" if row.get("odd") is None else
                            "DIAGNOSTIC_UNCALIBRATED" if row.get("calibration_status") != "CALIBRATED_OUT_OF_SAMPLE" else
                            "POSITIVE_CONSERVATIVE_EV" if (row.get("ev_conservative_low") or 0) > 0 else
                            "NO_POSITIVE_CONSERVATIVE_EV"
                        ),
                    },
                }
                for row in report["market_comparisons"]
            ],
        })
        summary["probabilities_computed_at_utc"] = report.get("probabilities_computed_at_utc")
        summary["snapshot_provenance"]["probabilities_computed_at_utc"] = summary["probabilities_computed_at_utc"]
        temporary = path.with_suffix(".validated.tmp")
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "status": "ERROR", "error": str(exc)}
