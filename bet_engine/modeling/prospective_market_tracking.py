"""Contract-level monitoring over immutable prospective predictions and settlements."""

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .calibration import brier_score, expected_calibration_error, log_loss
from .prospective_registry import (
    ProspectiveRegistryError, _canonical_sha256, default_settlement_path, extract_bwin_capture_utc,
)
from bet_engine.contracts.epistemic import parse_utc_timestamp


# Keys represent the same settlement predicate across fixtures, within one model.
CONTRACTS = {
    "home_win": "MATCH_RESULT_1X2", "draw": "MATCH_RESULT_1X2", "away_win": "MATCH_RESULT_1X2",
    "over_2_5_goals": "GOALS_FT", "under_2_5_goals": "GOALS_FT",
    "btts_yes": "BTTS", "btts_no": "BTTS",
}
HEADERS = {"resultado del partido", "total de goles", "ambos equipos marcan"}


def _read_record(path):
    raw = path.read_bytes()
    doc = json.loads(raw)
    pre = doc.get("pre_kickoff")
    if not pre or _canonical_sha256(pre) != doc.get("pre_kickoff_sha256"):
        raise ProspectiveRegistryError("PRE_KICKOFF_RECORD_TAMPERED")
    timing = pre["timing"]
    if (timing.get("temporal_integrity_status") != "PREMATCH_VERIFIED"
            or not parse_utc_timestamp(timing["cutoff_utc"]) <= parse_utc_timestamp(timing["generated_utc"])
            < parse_utc_timestamp(timing["kickoff_utc"])):
        raise ProspectiveRegistryError("INVALID_PROSPECTIVE_CLOCK")
    return doc, pre, hashlib.sha256(raw).hexdigest()


def _closing_path(record_path):
    return record_path.parent / "closing_odds" / f"{record_path.stem}.closing.json"


def attach_closing_odds(record_path: Path, snapshot: Path, *, expected_sha256: str,
                        max_lead_minutes: int = 20):
    """Attach the supplied late PREMATCH snapshot, preserving the prediction bytes."""
    from bet_engine.markets.bwin_catalog import extract_bwin_catalog

    if not 1 <= max_lead_minutes <= 20:
        raise ProspectiveRegistryError("INVALID_CLOSING_WINDOW")
    _, pre, record_sha = _read_record(record_path)
    raw = snapshot.read_bytes()
    snapshot_sha = hashlib.sha256(raw).hexdigest()
    if snapshot_sha != expected_sha256:
        raise ProspectiveRegistryError("CLOSING_SNAPSHOT_SHA_MISMATCH")
    eid = pre.get("event_id")
    if not eid:
        raise ProspectiveRegistryError("CLOSING_EVENT_ID_MISSING")
    captured = extract_bwin_capture_utc(snapshot)
    if not captured:
        raise ProspectiveRegistryError("CLOSING_CAPTURE_TIME_MISSING")
    cap = parse_utc_timestamp(captured)
    timing = pre["timing"]
    lead = (parse_utc_timestamp(timing["kickoff_utc"]) - cap).total_seconds() / 60
    if (not 0 < lead <= max_lead_minutes or cap <= parse_utc_timestamp(timing["generated_utc"])
            or cap > datetime.now(timezone.utc)):
        raise ProspectiveRegistryError("CLOSING_CAPTURE_OUTSIDE_WINDOW_OR_BEFORE_PREDICTION")
    teams = pre.get("teams") or {}
    catalog = extract_bwin_catalog(snapshot, event_id=str(eid), home_name=teams.get("home_name"),
                                   away_name=teams.get("away_name"))
    if catalog["source_txt_sha256"] != snapshot_sha or snapshot.read_bytes() != raw:
        raise ProspectiveRegistryError("CLOSING_SNAPSHOT_CHANGED")
    quotes = {}
    for row in catalog["entries"]:
        key = row["canonical_key"]
        parts = [part.strip() for part in row["market_header_literal"].casefold().split("|")]
        if (parts[0] in HEADERS and parts[1:] in ([], ["tiempo reglamentario"]) and key in CONTRACTS
                and row["temporal_scope"] == "FT" and row["odd"] and row["settlement_rules_available"]):
            quotes[key] = float(row["odd"])
    if not quotes:
        raise ProspectiveRegistryError("NO_COMPARABLE_CLOSING_QUOTES")
    data = {"record_file_sha256": record_sha, "event_id": str(eid), "captured_utc": captured,
            "snapshot_path": str(snapshot.resolve()), "snapshot_sha256": snapshot_sha,
            "lead_minutes": lead, "quotes": quotes, "status": "LATE_PREMATCH_SNAPSHOT"}
    path = _closing_path(record_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps({"closing": data, "closing_sha256": _canonical_sha256(data)}, indent=2).encode()
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError:
        existing = json.loads(path.read_bytes())
        prior = existing.get("closing") or {}
        if (existing.get("closing_sha256") != _canonical_sha256(prior)
                or prior.get("record_file_sha256") != record_sha or prior.get("snapshot_sha256") != snapshot_sha):
            raise ProspectiveRegistryError("IMMUTABLE_CLOSING_SNAPSHOT_ALREADY_EXISTS")
    return {**data, "path": str(path)}


def evaluate_market_tracking(records_dir: Path):
    groups = defaultdict(list)
    counts = Counter()
    seen = set()
    for path in sorted(records_dir.glob("*.json")):
        if path.name == "capture_and_exclusion_log.json":
            continue
        counts["records_seen"] += 1
        try:
            doc, pre, digest = _read_record(path)
            settlement_path = default_settlement_path(path)
            if not settlement_path.is_file():
                counts["pending_settlement"] += 1
                continue
            settled = json.loads(settlement_path.read_bytes())
            if (settled.get("record_file_sha256") != digest
                    or settled.get("pre_kickoff_sha256") != doc["pre_kickoff_sha256"]):
                raise ValueError("SETTLEMENT_BINDING_MISMATCH")
            result = settled["settlement"]
            if (result.get("status") != "SETTLED" or result.get("verification_status") != "VERIFIED_AUTHORITATIVE"
                    or not result.get("creditable") or not result.get("provider_provenance_verified")
                    or result.get("is_simulation") or result.get("match_status") != "FT"):
                counts["settlement_not_comparable"] += 1
                continue
            if not pre.get("eligible_for_paired_eval", True):
                counts["prediction_excluded"] += 1
                continue
            fixture = pre["fixture_id"]
            if fixture in seen:
                counts["duplicate_fixture"] += 1
                continue
            hg, ag = result["hg"], result["ag"]
            if type(hg) is not int or type(ag) is not int or min(hg, ag) < 0:
                raise ValueError("INVALID_SETTLEMENT_SCORE")
            seen.add(fixture)
            counts["comparable_fixtures"] += 1
        except (OSError, ValueError, KeyError, TypeError, ProspectiveRegistryError):
            counts["invalid_record_or_settlement"] += 1
            continue
        outcomes = dict(home_win=hg > ag, draw=hg == ag, away_win=hg < ag,
                        over_2_5_goals=hg+ag > 2.5, under_2_5_goals=hg+ag < 2.5,
                        btts_yes=hg > 0 and ag > 0, btts_no=not (hg > 0 and ag > 0))
        closing = {}
        closing_path = _closing_path(path)
        if closing_path.is_file():
            try:
                sidecar = json.loads(closing_path.read_bytes())
                data = sidecar["closing"]
                if data["record_file_sha256"] != digest or _canonical_sha256(data) != sidecar["closing_sha256"]:
                    raise ValueError("CLOSING_BINDING_MISMATCH")
                if hashlib.sha256(Path(data["snapshot_path"]).read_bytes()).hexdigest() != data["snapshot_sha256"]:
                    raise ValueError("CLOSING_SNAPSHOT_SHA_MISMATCH")
                closing = data["quotes"]
            except (OSError, ValueError, KeyError, TypeError):
                counts["invalid_closing_snapshot"] += 1
        else:
            counts["no_closing_snapshot"] += 1
        opening = pre.get("bwin_odds") or {}
        opening_quotes = opening.get("quotes", {}) if opening.get("odds_status") == "ODDS_VERIFIED" else {}
        version = (pre.get("provenance") or {}).get("manifest_sha256") or "UNSPECIFIED"
        for model, prediction in pre.get("predictions", {}).items():
            probabilities = dict(prediction.get("outcomes_1x2") or {})
            for field, yes, no in (("p_over_2_5", "over_2_5_goals", "under_2_5_goals"),
                                   ("p_btts_yes", "btts_yes", "btts_no")):
                p = prediction.get(field)
                if isinstance(p, (int, float)) and 0 <= p <= 1:
                    probabilities.update({yes: p, no: 1 - p})
            for key, family in CONTRACTS.items():
                p = probabilities.get(key)
                if not isinstance(p, (int, float)) or not 0 <= p <= 1:
                    continue
                start, end = opening_quotes.get(key), closing.get(key)
                clv = None
                if all(isinstance(o, (int, float)) and math.isfinite(o) and o > 1 for o in (start, end)):
                    clv = start / end - 1
                groups[(model, version, family, key)].append((fixture, p, int(outcomes[key]), clv))

    def metrics(rows):
        pairs = [(p, y) for _, p, y, _ in rows]
        clv = [v for _, _, _, v in rows if v is not None]
        return {"fixtures_n": len({row[0] for row in rows}), "selections_n": len(rows),
                "brier": brier_score(pairs), "log_loss": log_loss(pairs),
                "ece": expected_calibration_error(pairs), "clv_pairs_n": len(clv),
                "mean_raw_clv": round(sum(clv)/len(clv), 6) if clv else None}

    families = defaultdict(list)
    for (model, version, family, _), rows in groups.items():
        families[model, version, family].extend(rows)
    return {"status": "MONITORING_ONLY_NO_CALIBRATION_ACCREDITATION" if groups else "NO_COMPARABLE_RESULTS",
            "counts": dict(counts),
            "by_family": [{"model": m, "manifest_sha256": v, "family": f, **metrics(rows)}
                          for (m, v, f), rows in sorted(families.items())],
            "by_contract": [{"model": m, "manifest_sha256": v, "family": f, "selection_key": k, **metrics(rows)}
                            for (m, v, f, k), rows in sorted(groups.items())]}
