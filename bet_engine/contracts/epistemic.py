"""Contratos epistémicos versionados (v1.0) para separar hechos, patrones,
hipótesis tácticas, evidencia contextual, patrones validados y variables de modelo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


CONTRACT_SCHEMA_VERSION = "1.0"

ALLOWED_DSL_OPS = frozenset({"all", "any"})
ALLOWED_DSL_CMPS = frozenset({"gt", "ge", "lt", "le", "eq", "ne", "in", "not_in"})


class EpistemicContractError(ValueError):
    """Error de violación de contrato epistémico."""


class TemporalLeakageError(EpistemicContractError):
    """Error por fuga temporal (dato posterior al corte PREMATCH)."""


class CoverageFlag(str, Enum):
    """Cobertura declarada u observada: nunca asumir TRUE sin verificación."""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class PatternScope(str, Enum):
    """Distingue triggers observables PREMATCH de mecanismos internos históricos."""

    PREMATCH_TRIGGER = "PREMATCH_TRIGGER"
    INTRA_MATCH_MECHANISM = "INTRA_MATCH_MECHANISM"
    DESCRIPTIVE_SPLIT = "DESCRIPTIVE_SPLIT"


class PrematchAvailability(str, Enum):
    """Disponibilidad de las variables de una hipótesis antes del kickoff."""

    PREMATCH_OBSERVABLE = "PREMATCH_OBSERVABLE"
    POSTMATCH_ONLY = "POSTMATCH_ONLY"
    MIXED = "MIXED"


class VerificationStatus(str, Enum):
    """Nivel de acreditación de una evidencia contextual."""

    CONFIRMADO_OFICIAL = "CONFIRMADO_OFICIAL"
    PUBLICADO_PREPARTIDO = "PUBLICADO_PREPARTIDO"
    PROBABLE = "PROBABLE"
    INFERIDO = "INFERIDO"
    NO_VERIFICADO = "NO_VERIFICADO"


def parse_utc_timestamp(raw: str | datetime) -> datetime:
    """Convierte una fecha ISO-8601 a datetime aware en UTC."""
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise TemporalLeakageError(f"TIMESTAMP_INVALIDO: {raw!r}")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def assert_prematch_cutoff(
    available_at_utc: str | datetime,
    cutoff_utc: str | datetime,
    label: str = "record",
) -> None:
    """Garantiza estrictamente que available_at_utc <= cutoff_utc."""
    avail = parse_utc_timestamp(available_at_utc)
    cutoff = parse_utc_timestamp(cutoff_utc)
    if avail > cutoff:
        raise TemporalLeakageError(
            f"TEMPORAL_LEAKAGE({label}): available_at_utc={avail.isoformat()} > cutoff_utc={cutoff.isoformat()}"
        )


def validate_declarative_dsl(node: dict[str, Any]) -> None:
    """Valida que un predicado python_test sea DSL declarativo puro y nunca código Python."""
    if not isinstance(node, dict):
        raise EpistemicContractError("DSL_MUST_BE_DICT")
    if "code" in node or "eval" in node or "exec" in node or "lambda" in node:
        raise EpistemicContractError("ARBITRARY_PYTHON_CODE_FORBIDDEN_IN_DSL")
    op = node.get("op")
    if op not in ALLOWED_DSL_OPS:
        raise EpistemicContractError(f"DSL_INVALID_OP: {op!r}")
    clauses = node.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        raise EpistemicContractError("DSL_EMPTY_CLAUSES")
    for item in clauses:
        if not isinstance(item, dict):
            raise EpistemicContractError("DSL_CLAUSE_MUST_BE_DICT")
        if "op" in item:
            validate_declarative_dsl(item)
            continue
        field_name = item.get("field")
        cmp_op = item.get("cmp")
        if not isinstance(field_name, str) or not field_name.strip():
            raise EpistemicContractError(f"DSL_INVALID_FIELD: {field_name!r}")
        if cmp_op not in ALLOWED_DSL_CMPS:
            raise EpistemicContractError(f"DSL_INVALID_CMP: {cmp_op!r}")
        if "value" not in item and "value_field" not in item:
            raise EpistemicContractError("DSL_CLAUSE_REQUIRES_VALUE_OR_VALUE_FIELD")


@dataclass(frozen=True)
class ObservedFact:
    """1. Dato observado primario. Regla estricta: no cubierto => value=None (nunca 0)."""

    fact_id: str
    entity_type: str  # TEAM | PLAYER | REFEREE | MATCH
    entity_id: int
    fixture_id: int
    kickoff_utc: str
    available_at_utc: str
    competition_id: int
    season: int
    competition_stratum: str  # DOMESTIC_LEAGUE | DOMESTIC_CUP | CONTINENTAL_CLUB | NATIONAL_TEAM | UNKNOWN
    period: str  # FT | H1 | H2 | P_00_15 ...
    metric_key: str
    value: float | int | str | bool | None
    is_covered: bool
    coverage_declared: CoverageFlag = CoverageFlag.UNKNOWN
    context_tags: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: str = CONTRACT_SCHEMA_VERSION
    contract_type: str = "ObservedFact"

    def __post_init__(self) -> None:
        parse_utc_timestamp(self.kickoff_utc)
        parse_utc_timestamp(self.available_at_utc)
        if not self.is_covered and self.value is not None:
            raise EpistemicContractError(
                f"MISSING_COVERAGE_MUST_KEEP_NULL({self.fact_id}): is_covered=False pero value={self.value!r}"
            )
        if self.is_covered and self.value is None:
            raise EpistemicContractError(
                f"COVERED_FACT_CANNOT_BE_NONE({self.fact_id}): is_covered=True pero value=None"
            )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["coverage_declared"] = self.coverage_declared.value
        return data


@dataclass(frozen=True)
class StatisticalPattern:
    """2. Patrón estadístico descriptivo o exploratorio. No autoriza ajuste de p_model."""

    pattern_id: str
    pattern_scope: PatternScope
    target_metric: str
    condition_dsl: dict[str, Any] | None
    sample_total: int
    sample_valid: int
    coverage_observed_pct: float
    estimates: dict[str, Any]
    significance: dict[str, Any] = field(default_factory=dict)
    supporting_fixture_ids: tuple[int, ...] = ()
    counterexample_fixture_ids: tuple[int, ...] = ()
    contradiction_flags: tuple[str, ...] = ()
    competition_mix: dict[str, float] = field(default_factory=dict)
    exploratory_only: bool = True
    schema_version: str = CONTRACT_SCHEMA_VERSION
    contract_type: str = "StatisticalPattern"

    def __post_init__(self) -> None:
        if self.sample_valid > self.sample_total:
            raise EpistemicContractError("SAMPLE_VALID_CANNOT_EXCEED_SAMPLE_TOTAL")
        if not (0.0 <= self.coverage_observed_pct <= 100.0):
            raise EpistemicContractError("COVERAGE_PCT_OUT_OF_RANGE")
        if self.condition_dsl is not None:
            validate_declarative_dsl(self.condition_dsl)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pattern_scope"] = self.pattern_scope.value
        return data


@dataclass(frozen=True)
class ContextualEvidence:
    """3. Evidencia contextual prepartido con nivel de verificación explícito."""

    evidence_id: str
    fixture_id: int
    category: str  # SCHEDULE_FATIGUE | COACH_REGIME | LINEUP_DELTA | INJURY_ABSENCE | REFEREE_PROFILE | STANDINGS_INCENTIVE | RED_CARD_DISTORTION
    verification_status: VerificationStatus
    recorded_at_utc: str
    facts: dict[str, Any]
    regime_invalidation: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: str = CONTRACT_SCHEMA_VERSION
    contract_type: str = "ContextualEvidence"

    def __post_init__(self) -> None:
        parse_utc_timestamp(self.recorded_at_utc)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verification_status"] = self.verification_status.value
        return data


@dataclass(frozen=True)
class TacticalHypothesis:
    """4. Hipótesis táctica causal (de plantilla Python o de investigador LLM Codex)."""

    hypothesis_id: str
    origin: str  # PYTHON_TACTICAL_TEMPLATE | LLM_INVESTIGATOR_CODEX
    prematch_availability: PrematchAvailability
    causal_chain: dict[str, str]
    target_market_families: tuple[str, ...]
    python_test: dict[str, Any] | None = None
    validation_state: str = "PENDING_PYTHON_EVALUATION"
    schema_version: str = CONTRACT_SCHEMA_VERSION
    contract_type: str = "TacticalHypothesis"

    def __post_init__(self) -> None:
        if self.python_test is not None:
            validate_declarative_dsl(self.python_test)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["prematch_availability"] = self.prematch_availability.value
        return data


@dataclass(frozen=True)
class ValidatedPattern:
    """5. Patrón validado temporalmente. Puede priorizar candidatos, no alterar p_model sin benchmark."""

    validated_id: str
    source_pattern_id: str
    source_hypothesis_id: str | None
    prematch_availability: PrematchAvailability
    validation_protocol: str  # TEMPORAL_SPLIT_BY_FDR | WALK_FORWARD_OUT_OF_SAMPLE | LOOCV_EXPLORATORY_ONLY
    train_metrics: dict[str, Any]
    holdout_metrics: dict[str, Any]
    regime_check: dict[str, Any]
    authorized_for_candidate_ranking: bool
    authorized_for_probabilistic_adjustment: bool = False
    schema_version: str = CONTRACT_SCHEMA_VERSION
    contract_type: str = "ValidatedPattern"

    def __post_init__(self) -> None:
        if self.authorized_for_probabilistic_adjustment:
            raise EpistemicContractError(
                "VALIDATED_PATTERN_CANNOT_DIRECTLY_ALTER_P_MODEL: requiere promoción explícita a AcceptedModelFeature"
            )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["prematch_availability"] = self.prematch_availability.value
        return data


@dataclass(frozen=True)
class AcceptedModelFeature:
    """6. Variable aceptada por un modelo probabilístico tras mejora out-of-sample."""

    feature_id: str
    source_validated_id: str
    prematch_availability: PrematchAvailability
    market_family: str
    out_of_sample_improvement: dict[str, float]  # brier_delta, log_loss_delta, mae_delta
    shrinkage_weight: float
    catalog_version: str
    schema_version: str = CONTRACT_SCHEMA_VERSION
    contract_type: str = "AcceptedModelFeature"

    def __post_init__(self) -> None:
        if self.prematch_availability != PrematchAvailability.PREMATCH_OBSERVABLE:
            raise EpistemicContractError(
                f"FEATURE_MUST_BE_PREMATCH_OBSERVABLE: recibido {self.prematch_availability.value}"
            )
        if not (0.0 < self.shrinkage_weight <= 1.0):
            raise EpistemicContractError("SHRINKAGE_WEIGHT_OUT_OF_RANGE")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["prematch_availability"] = self.prematch_availability.value
        return data


def validate_epistemic_promotion(
    source: StatisticalPattern | TacticalHypothesis | ValidatedPattern,
    target_type: str,
) -> None:
    """Verifica reglas estrictas antes de promover un objeto entre categorías epistémicas."""
    if target_type == "AcceptedModelFeature":
        if not isinstance(source, ValidatedPattern):
            raise EpistemicContractError("ONLY_VALIDATED_PATTERN_CAN_PROMOTE_TO_ACCEPTED_FEATURE")
        if source.prematch_availability != PrematchAvailability.PREMATCH_OBSERVABLE:
            raise EpistemicContractError("POSTMATCH_OR_MIXED_PATTERN_CANNOT_BECOME_MODEL_FEATURE")
        if source.validation_protocol == "LOOCV_EXPLORATORY_ONLY":
            raise EpistemicContractError("EXPLORATORY_LOOCV_CANNOT_PROMOTE_TO_MODEL_FEATURE")
    elif target_type == "ValidatedPattern":
        if isinstance(source, TacticalHypothesis) and source.validation_state == "PENDING_PYTHON_EVALUATION":
            raise EpistemicContractError("HYPOTHESIS_MUST_BE_EVALUATED_BY_PYTHON_BEFORE_VALIDATION")
    else:
        raise EpistemicContractError(f"UNKNOWN_PROMOTION_TARGET: {target_type}")
