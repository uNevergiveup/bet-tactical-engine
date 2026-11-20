"""MVP importador de Quick Context cualitativo PREMATCH en Markdown."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bet_engine.contracts.epistemic import (
    ContextualEvidence,
    EpistemicContractError,
    TemporalLeakageError,
    VerificationStatus,
    parse_utc_timestamp,
)

QUICK_CONTEXT_SCHEMA = "1.0-quick-context-mvp"

_TAG_MAP = {
    "CONFIRMADO": VerificationStatus.CONFIRMADO_OFICIAL,
    "CONFIRMADO_OFICIAL": VerificationStatus.CONFIRMADO_OFICIAL,
    "PUBLICADO_PREPARTIDO": VerificationStatus.PUBLICADO_PREPARTIDO,
    "PUBLICADO": VerificationStatus.PUBLICADO_PREPARTIDO,
    "PROBABLE": VerificationStatus.PROBABLE,
    "INFERIDO": VerificationStatus.INFERIDO,
    "NO_VERIFICADO": VerificationStatus.NO_VERIFICADO,
    "HIPOTESIS_TIPSTER": VerificationStatus.NO_VERIFICADO,
    "TIPSTER": VerificationStatus.NO_VERIFICADO,
    "RUMOR": VerificationStatus.NO_VERIFICADO,
    "CONTRADICCION": VerificationStatus.NO_VERIFICADO,
}

_HYPOTHESIS_AFFECTED_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("cesped artificial", "artificial turf", "astana arena", "superficie", "sintetico"),
        "SURFACE_ARTIFICIAL_TURF",
        "Afecta supuestos de bote, velocidad de balón, tiros bloqueados, córners y riesgo físico/conservación respecto a partidos sobre hierba natural.",
    ),
    (
        ("rotacion", "rotaciones", "descansan", "descanso", "no viajaron", "suplentes", "bajas", "titulares"),
        "LINEUP_ROTATION_EXPOSURE",
        "Afecta producción ofensiva/defensiva histórica (L5/L20), xG esperado y props de jugador hasta confirmar el XI oficial.",
    ),
    (
        ("ascenso", "liderato", "clasificacion", "ya clasificado", "sin jugarse", "motivacion", "intrascendente"),
        "COMPETITIVE_INCENTIVE_SHIFT",
        "Afecta hipótesis de intensidad competitiva, faltas, tarjetas, presión alta y gestión del marcador tras adelantarse.",
    ),
    (
        ("viaje", "desplazamiento", "huso horario", "altitud", "clima", "frio", "viento"),
        "TRAVEL_CLIMATE_FATIGUE",
        "Afecta ritmo de juego, caída física en 2ª parte y frecuencia de duelos/faltas.",
    ),
    (
        ("tipster", "xi confirmado", "xi probable", "convocados", "no confirmado"),
        "XI_NOT_CONFIRMED_EXPLORATORY",
        "Bloquea inferencias fuertes dependientes de titulares mientras no se publiquen alineaciones oficiales.",
    ),
)

_TEAM_TOKEN_ALIASES: dict[str, set[str]] = {
    "kazakhstan": {"kazakhstan", "kazajistan", "kazajstan"},
    "kazajistan": {"kazakhstan", "kazajistan", "kazajstan"},
    "slovakia": {"slovakia", "eslovaquia"},
    "eslovaquia": {"slovakia", "eslovaquia"},
    "estonia": {"estonia"},
    "bulgaria": {"bulgaria"},
    "spain": {"spain", "espana"},
    "germany": {"germany", "alemania"},
    "netherlands": {"netherlands", "paises", "bajos", "holanda"},
    "greece": {"greece", "grecia"},
    "serbia": {"serbia"},
    "iceland": {"iceland", "islandia"},
    "luxembourg": {"luxembourg", "luxemburgo"},
    "faroe": {"faroe", "feroe"},
    "moldova": {"moldova", "moldavia"},
}


def _strip_accents(value: str) -> str:
    norm = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).lower()


def _team_tokens(name: str) -> set[str]:
    base = {tok for tok in re.findall(r"[a-z0-9]+", _strip_accents(name)) if len(tok) >= 3}
    expanded = set(base)
    for tok in base:
        expanded.update(_TEAM_TOKEN_ALIASES.get(tok, ()))
    return expanded


def _teams_compatible(actual: str, expected: str) -> bool:
    if not actual or not expected:
        return False
    a = _team_tokens(actual)
    b = _team_tokens(expected)
    if a & b:
        return True
    return any(x[:4] == y[:4] for x in a for y in b if len(x) >= 4 and len(y) >= 4)


def _extract_field(raw: str, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        pattern = rf"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?{re.escape(key)}(?:\*\*)?\s*:\s*(.+?)\s*$"
        match = re.search(pattern, raw)
        if match:
            val = re.sub(r"^\*\*|\*\*$", "", match.group(1)).strip().strip("`")
            if val:
                return val
    return None


def _parse_iso(raw: str | None, label: str) -> datetime:
    if not raw:
        raise EpistemicContractError(f"QUICK_CONTEXT_MISSING_TIMESTAMP:{label}")
    m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})", raw)
    if not m:
        raise EpistemicContractError(f"QUICK_CONTEXT_INVALID_ISO_TIMESTAMP:{label}:{raw}")
    return parse_utc_timestamp(m.group(0))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _section_default_status(heading: str) -> VerificationStatus | str | None:
    h = _strip_accents(heading)
    if "confirmad" in h or "oficial" in h:
        return VerificationStatus.CONFIRMADO_OFICIAL
    if "publicad" in h:
        return VerificationStatus.PUBLICADO_PREPARTIDO
    if "probable" in h:
        return VerificationStatus.PROBABLE
    if "inferid" in h or "implicacion" in h:
        return VerificationStatus.INFERIDO
    if "no verificad" in h or "hipotesis" in h or "tipster" in h or "rumor" in h:
        return VerificationStatus.NO_VERIFICADO
    if "contradiccion" in h:
        return "CONTRADICTION_SECTION"
    return None


def parse_quick_context_markdown(
    raw_md: str,
    *,
    expected_fixture_id: int,
    expected_event_id: str | None,
    expected_home: str,
    expected_away: str,
    expected_kickoff_utc: str,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Valida identidad, timestamps, fuentes, contradicciones y construye hallazgos sin ejecutar código."""
    if not raw_md or not raw_md.strip():
        raise EpistemicContractError("QUICK_CONTEXT_EMPTY_MARKDOWN")

    fixture_raw = _extract_field(raw_md, ("fixture_id", "fixture_id_api", "Fixture ID"))
    if not fixture_raw or not re.search(r"\d+", fixture_raw):
        raise EpistemicContractError("QUICK_CONTEXT_MISSING_FIXTURE_ID")
    parsed_fid = int(re.search(r"\d+", fixture_raw).group(0))
    if parsed_fid != int(expected_fixture_id):
        raise EpistemicContractError(f"QUICK_CONTEXT_FIXTURE_MISMATCH:{parsed_fid}!={expected_fixture_id}")

    bwin_raw = _extract_field(raw_md, ("bwin_event_id", "event_id_bwin", "Event ID Bwin", "Event ID"))
    parsed_eid: str | None = None
    if bwin_raw:
        digits = re.search(r"\b(\d{6,})\b", bwin_raw)
        if digits:
            parsed_eid = digits.group(1)
            if expected_event_id and parsed_eid != str(expected_event_id):
                raise EpistemicContractError(f"QUICK_CONTEXT_EVENT_ID_MISMATCH:{parsed_eid}!={expected_event_id}")

    home_raw = _extract_field(raw_md, ("home", "local", "Home"))
    away_raw = _extract_field(raw_md, ("away", "visitante", "Away"))
    partido_raw = _extract_field(raw_md, ("partido", "match", "Partido"))
    if (not home_raw or not away_raw) and partido_raw:
        parts = re.split(r"\s+(?:vs\.?|v\.?|-|–|—)\s+", partido_raw, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            home_raw = home_raw or parts[0].strip()
            away_raw = away_raw or parts[1].strip()
    if not home_raw or not away_raw:
        heading = re.search(r"(?m)^#\s+(.+?)\s*$", raw_md)
        if heading:
            cleaned_h = re.sub(r"^[^:]+:\s*", "", heading.group(1))
            parts = re.split(r"\s+(?:vs\.?|v\.?|-|–|—)\s+", cleaned_h, maxsplit=1, flags=re.I)
            if len(parts) == 2:
                home_raw = home_raw or parts[0].strip()
                away_raw = away_raw or parts[1].strip()

    if not _teams_compatible(home_raw or "", expected_home) or not _teams_compatible(away_raw or "", expected_away):
        raise EpistemicContractError(
            f"QUICK_CONTEXT_TEAM_IDENTITY_MISMATCH:({home_raw} vs {away_raw})!=({expected_home} vs {expected_away})"
        )

    competition_raw = _extract_field(raw_md, ("competition", "competicion", "Competición", "league"))
    if not competition_raw:
        raise EpistemicContractError("QUICK_CONTEXT_MISSING_COMPETITION")

    ko_expected = parse_utc_timestamp(expected_kickoff_utc)
    ko_raw = _extract_field(raw_md, ("kickoff_utc", "kickoff_madrid", "fecha_hora_encuentro", "Kickoff"))
    ko_doc = _parse_iso(ko_raw, "kickoff")
    if abs((ko_doc - ko_expected).total_seconds()) > 120:
        raise TemporalLeakageError(
            f"QUICK_CONTEXT_KICKOFF_CONFLICT:{ko_doc.isoformat()}!={ko_expected.isoformat()}"
        )

    research_raw = _extract_field(raw_md, ("research_timestamp_utc", "research_generated_utc", "hora_investigacion_utc"))
    cutoff_raw = _extract_field(raw_md, ("analysis_cutoff_utc", "cutoff_utc", "corte_temporal_utc"))
    research_dt = _parse_iso(research_raw, "research_timestamp_utc")
    cutoff_dt = _parse_iso(cutoff_raw, "analysis_cutoff_utc")

    now_ref = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if cutoff_dt >= ko_expected:
        raise TemporalLeakageError(f"QUICK_CONTEXT_CUTOFF_AT_OR_AFTER_KICKOFF:{cutoff_dt.isoformat()}")
    if research_dt >= ko_expected:
        raise TemporalLeakageError(f"QUICK_CONTEXT_RESEARCH_AT_OR_AFTER_KICKOFF:{research_dt.isoformat()}")
    if cutoff_dt > now_ref or research_dt > now_ref:
        raise TemporalLeakageError("QUICK_CONTEXT_FUTURE_TIMESTAMP_REJECTED")
    if cutoff_dt < research_dt - __import__("datetime").timedelta(seconds=60):
        raise TemporalLeakageError("QUICK_CONTEXT_CUTOFF_BEFORE_RESEARCH_TIMESTAMP")

    urls = sorted(set(re.findall(r"https?://[^\s\])>\"']+", raw_md)))
    if not urls:
        raise EpistemicContractError("QUICK_CONTEXT_MISSING_SOURCE_URLS")

    # Rechazar fechas explícitas posteriores al cutoff en líneas de fuentes/hallazgos.
    for line_no, line in enumerate(raw_md.splitlines(), 1):
        if any(k in line.lower() for k in ("kickoff", "fecha_hora_encuentro", "programad")):
            continue
        for ts_match in re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", line):
            ts_dt = parse_utc_timestamp(ts_match)
            if ts_dt > cutoff_dt:
                raise TemporalLeakageError(
                    f"QUICK_CONTEXT_POST_CUTOFF_EVIDENCE_LINE_{line_no}:{ts_dt.isoformat()}>{cutoff_dt.isoformat()}"
                )

    current_heading = ""
    findings: list[dict[str, Any]] = []
    contradictions: list[str] = []
    affected_map: dict[str, dict[str, Any]] = {}

    for line in raw_md.splitlines():
        h_match = re.match(r"^\s*#{1,4}\s+(.+?)\s*$", line)
        if h_match:
            current_heading = h_match.group(1).strip()
            continue
        b_match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if not b_match:
            continue
        item = b_match.group(1).strip()
        if re.match(r"^(?:\*\*)?(?:fixture_id|bwin_event_id|event_id_bwin|home|away|partido|competicion|competition|kickoff_|research_|analysis_cutoff|cutoff_)", _strip_accents(item)):
            continue
        tag_match = re.match(r"^\[([A-Z_]+)\]\s*(.+)$", item)
        tag = tag_match.group(1) if tag_match else None
        text = tag_match.group(2).strip() if tag_match else item

        section_status = _section_default_status(current_heading)
        if tag == "CONTRADICCION" or section_status == "CONTRADICTION_SECTION":
            contradictions.append(text)
            continue
        fallback = section_status if isinstance(section_status, VerificationStatus) else VerificationStatus.NO_VERIFICADO
        status = _TAG_MAP.get(tag or "", fallback)

        item_urls = re.findall(r"https?://[^\s\])>\"']+", text)
        item_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}))?\b", text)
        findings.append(
            {
                "section": current_heading,
                "status": status.value,
                "tag": tag,
                "text": text,
                "urls": item_urls,
                "dates": item_dates,
            }
        )

        norm_text = _strip_accents(text)
        for keywords, h_id, impact in _HYPOTHESIS_AFFECTED_RULES:
            if any(kw in norm_text for kw in keywords):
                entry = affected_map.setdefault(
                    h_id,
                    {"hypothesis_area": h_id, "impact_note": impact, "triggering_statuses": set(), "matched_items": []},
                )
                entry["triggering_statuses"].add(status.value)
                entry["matched_items"].append(text[:160])

    # Detección automática de contradicción si conviven rumores/hipótesis de bajas masivas sin XI oficial.
    has_unverified_mass_rotation = any(
        f["status"] == VerificationStatus.NO_VERIFICADO.value
        and any(tok in _strip_accents(f["text"]) for tok in ("tipster", "descansan", "doce titulares", "12 titulares", "masiva"))
        for f in findings
    )
    has_confirmed_xi = any(
        f["status"] == VerificationStatus.CONFIRMADO_OFICIAL.value and "xi confirmado" in _strip_accents(f["text"])
        for f in findings
    )
    if has_unverified_mass_rotation and not has_confirmed_xi:
        auto_c = "AUTO_DETECTADA: Hipótesis/rumor de rotación masiva (ej. tipster) sin XI oficial confirmado en fuentes primarias."
        if auto_c not in contradictions:
            contradictions.append(auto_c)

    if not findings:
        raise EpistemicContractError("QUICK_CONTEXT_NO_STRUCTURED_FINDINGS")

    affected_hypotheses = [
        {
            "hypothesis_area": v["hypothesis_area"],
            "impact_note": v["impact_note"],
            "triggering_statuses": sorted(v["triggering_statuses"]),
            "matched_count": len(v["matched_items"]),
        }
        for v in affected_map.values()
    ]

    return {
        "schema_version": QUICK_CONTEXT_SCHEMA,
        "fixture_id": parsed_fid,
        "bwin_event_id": parsed_eid,
        "home": home_raw,
        "away": away_raw,
        "competition": competition_raw,
        "kickoff_utc": ko_doc.isoformat(),
        "research_timestamp_utc": research_dt.isoformat(),
        "analysis_cutoff_utc": cutoff_dt.isoformat(),
        "source_urls": urls,
        "findings": findings,
        "contradictions": contradictions,
        "affected_hypotheses": affected_hypotheses,
        "authorized_for_probabilistic_adjustment": False,
        "counts_by_status": {
            st: sum(1 for f in findings if f["status"] == st)
            for st in sorted({f["status"] for f in findings})
        },
    }


def _category_for_finding(text: str) -> str:
    norm = _strip_accents(text)
    if any(k in norm for k in ("xi", "titular", "alineacion", "convocad", "rotacion", "suplente")):
        return "LINEUP_DELTA"
    if any(k in norm for k in ("baja", "lesion", "sancion", "no viaj")):
        return "INJURY_ABSENCE"
    if any(k in norm for k in ("ascenso", "clasific", "motivacion", "descenso", "playoff")):
        return "STANDINGS_INCENTIVE"
    if any(k in norm for k in ("cesped", "viaje", "fatiga", "descanso", "astana", "estadio")):
        return "SCHEDULE_FATIGUE"
    if any(k in norm for k in ("tactic", "tecnic", "seleccionador", "declaracion")):
        return "COACH_REGIME"
    return "STANDINGS_INCENTIVE"


def build_contextual_evidences(parsed: dict[str, Any], raw_sha256: str) -> list[ContextualEvidence]:
    """Construye contratos ContextualEvidence trazables sin alterar p_model."""
    evidences: list[ContextualEvidence] = []
    cutoff_utc = parsed["analysis_cutoff_utc"]
    fixture_id = int(parsed["fixture_id"])
    for idx, f in enumerate(parsed["findings"], 1):
        status = VerificationStatus(f["status"])
        ev = ContextualEvidence(
            evidence_id=f"qc_{raw_sha256[:10]}_{idx}",
            fixture_id=fixture_id,
            category=_category_for_finding(f["text"]),
            verification_status=status,
            recorded_at_utc=cutoff_utc,
            facts={
                "summary": f["text"],
                "section": f["section"],
                "dates": f["dates"],
                "authorized_for_probabilistic_adjustment": False,
                "affected_hypotheses": [h["hypothesis_area"] for h in parsed["affected_hypotheses"]],
            },
            regime_invalidation={"contradictions": parsed["contradictions"]} if parsed["contradictions"] else {},
            provenance={
                "source_type": "QUICK_CONTEXT_MARKDOWN",
                "quick_context_sha256": raw_sha256,
                "urls": f["urls"] or parsed["source_urls"][:3],
                "research_timestamp_utc": parsed["research_timestamp_utc"],
                "analysis_cutoff_utc": cutoff_utc,
                "overwrites_official_api_data": False,
            },
        )
        evidences.append(ev)
    return evidences


def import_quick_context_markdown(
    case_dir: Path,
    source_path: Path,
    *,
    expected_fixture_id: int,
    expected_event_id: str | None,
    expected_home: str,
    expected_away: str,
    expected_kickoff_utc: str,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Valida, deduplica y almacena de forma inmutable un Quick Context Markdown."""
    raw_bytes = source_path.read_bytes()
    raw_sha = _sha256_bytes(raw_bytes)
    raw_text = raw_bytes.decode("utf-8-sig")

    parsed = parse_quick_context_markdown(
        raw_text,
        expected_fixture_id=expected_fixture_id,
        expected_event_id=expected_event_id,
        expected_home=expected_home,
        expected_away=expected_away,
        expected_kickoff_utc=expected_kickoff_utc,
        now_utc=now_utc,
    )
    evidences = build_contextual_evidences(parsed, raw_sha)

    qc_root = case_dir / "research" / "quick_context"
    index_path = qc_root / "index.json"
    if index_path.is_file():
        index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index_doc = {
            "schema_version": QUICK_CONTEXT_SCHEMA,
            "fixture_id": int(expected_fixture_id),
            "bwin_event_id": expected_event_id,
            "items": [],
        }

    existing_entry = next((x for x in index_doc.get("items", []) if x.get("sha256") == raw_sha), None)
    if existing_entry is not None:
        return {
            "status": "ALREADY_IMPORTED_DUPLICATE_PREVENTED",
            "duplicate": True,
            "sha256": raw_sha,
            "directory": existing_entry["directory"],
            "evidence_count": len(evidences),
            "contradictions": parsed["contradictions"],
            "affected_hypotheses": parsed["affected_hypotheses"],
            "p_model_modified": False,
        }

    target_dir = qc_root / raw_sha
    target_dir.mkdir(parents=True, exist_ok=True)
    preserved_md = target_dir / source_path.name
    if not preserved_md.exists():
        with preserved_md.open("xb") as fh:
            fh.write(raw_bytes)

    parsed_payload = {**parsed, "sha256": raw_sha, "source_filename": source_path.name, "preserved_md": str(preserved_md)}
    evidences_payload = [ev.to_dict() for ev in evidences]
    (target_dir / "quick_context_parsed.json").write_text(
        json.dumps(parsed_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target_dir / "contextual_evidence.json").write_text(
        json.dumps(evidences_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    record = {
        "sha256": raw_sha,
        "imported_at_utc": (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "source_filename": source_path.name,
        "preserved_md": str(preserved_md.resolve()),
        "parsed_json": str((target_dir / "quick_context_parsed.json").resolve()),
        "contextual_evidence_json": str((target_dir / "contextual_evidence.json").resolve()),
        "directory": str(target_dir.resolve()),
        "analysis_cutoff_utc": parsed["analysis_cutoff_utc"],
        "research_timestamp_utc": parsed["research_timestamp_utc"],
        "evidence_count": len(evidences),
        "contradictions": parsed["contradictions"],
        "affected_hypotheses": parsed["affected_hypotheses"],
    }
    index_doc.setdefault("items", []).append(record)
    index_doc["latest_sha256"] = raw_sha
    tmp_index = index_path.with_name(index_path.name + ".tmp")
    tmp_index.write_text(json.dumps(index_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_index.replace(index_path)

    return {
        "status": "QUICK_CONTEXT_IMPORTED",
        "duplicate": False,
        "sha256": raw_sha,
        "directory": str(target_dir.resolve()),
        "preserved_md": str(preserved_md.resolve()),
        "index_path": str(index_path.resolve()),
        "evidence_count": len(evidences),
        "counts_by_status": parsed["counts_by_status"],
        "contradictions": parsed["contradictions"],
        "affected_hypotheses": parsed["affected_hypotheses"],
        "p_model_modified": False,
    }


def load_quick_context_bundle(case_dir: Path | None) -> tuple[list[ContextualEvidence], list[dict[str, Any]]]:
    """Carga contratos ContextualEvidence y metadatos Quick Context si existen; [] si no."""
    if case_dir is None:
        return [], []
    index_path = Path(case_dir) / "research" / "quick_context" / "index.json"
    if not index_path.is_file():
        return [], []
    index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    evidences: list[ContextualEvidence] = []
    summaries: list[dict[str, Any]] = []
    for item in index_doc.get("items", []):
        parsed_path = Path(item.get("parsed_json", ""))
        ev_path = Path(item.get("contextual_evidence_json", ""))
        if parsed_path.is_file():
            summaries.append(json.loads(parsed_path.read_text(encoding="utf-8")))
        if ev_path.is_file():
            for row in json.loads(ev_path.read_text(encoding="utf-8")):
                evidences.append(
                    ContextualEvidence(
                        evidence_id=row["evidence_id"],
                        fixture_id=int(row["fixture_id"]),
                        category=row["category"],
                        verification_status=VerificationStatus(row["verification_status"]),
                        recorded_at_utc=row["recorded_at_utc"],
                        facts=row["facts"],
                        regime_invalidation=row.get("regime_invalidation") or {},
                        provenance=row.get("provenance") or {},
                    )
                )
    return evidences, summaries
