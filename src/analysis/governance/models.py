from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    TypeAdapter,
    ValidationInfo,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from .canonical import (
    SHANGHAI_TIMEZONE,
    canonical_decimal,
    canonical_json_bytes,
    canonical_sha256,
    load_canonical_json,
    validate_canonical_json,
)


MODEL_SCHEMA_VERSION = "1.0.0"
GOVERNANCE_ACQUISITION_SCOPE = "governance_management"
GOVERNANCE_QUESTION_SET_ID = "governance_management_questions"


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _decimal_input(value: Any, _info: ValidationInfo) -> Decimal:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError("decimal fields reject int/float input; use Decimal or a decimal string")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid decimal string") from exc
    else:
        raise ValueError("decimal fields require Decimal or a decimal string")
    if not parsed.is_finite():
        raise ValueError("decimal fields must be finite")
    return parsed


def _sorted_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError("set-like identifiers must not contain duplicates")
    return tuple(sorted(values))


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError("ordered identifiers must not contain duplicates")
    return values


NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
QuestionId = Annotated[
    str,
    StringConstraints(pattern=r"^GOV\.Q(?:0[1-9]|1[01])\.[A-Z][A-Z0-9_]*$"),
]
Sha256Hex = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
UtcDateTime = Annotated[
    datetime,
    AwareDatetime,
    AfterValidator(_to_utc),
]
CanonicalDecimal = Annotated[
    Decimal,
    BeforeValidator(_decimal_input),
    PlainSerializer(canonical_decimal, return_type=str),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$",
        }
    ),
]
SortedUniqueStrings = Annotated[tuple[NonEmptyStr, ...], AfterValidator(_sorted_unique)]
OrderedUniqueStrings = Annotated[tuple[NonEmptyStr, ...], AfterValidator(_ordered_unique)]
SortedUniqueQuestionIds = Annotated[
    tuple[QuestionId, ...], AfterValidator(_sorted_unique)
]


class SourceRole(str, Enum):
    OFFICIAL_DISCLOSURE = "official_disclosure"
    REGULATOR_EXCHANGE = "regulator_exchange"
    DISCOVERY_ONLY = "discovery_only"
    CONTEXTUAL_EVIDENCE = "contextual_evidence"
    DEFERRED = "deferred"


class ExtractorKind(str, Enum):
    DETERMINISTIC = "deterministic"
    REGEX = "regex"
    LLM = "llm"


class ExtractionStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    CONFLICTED = "conflicted"
    NOT_APPLICABLE = "not_applicable"


class ReviewStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CompletenessStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    CONFLICTED = "conflicted"


class GovernancePerspective(str, Enum):
    STRICT = "strict"
    RECONSTRUCTED = "reconstructed"


class ReportGenerationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TimePrecision(str, Enum):
    DATE = "date"
    DATETIME = "datetime"


class ClaimObjectType(str, Enum):
    TEXT = "text"
    IDENTIFIER = "identifier"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    UNKNOWN = "unknown"


class DecisionValue(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RecordResolutionStatus(str, Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class DirectionKind(str, Enum):
    DIRECT = "direct"
    INDIRECT = "indirect"


class DeltaDisposition(str, Enum):
    APPLIED = "applied"
    EXCLUDED = "excluded"


class SnapshotRecordRole(str, Enum):
    CANONICAL = "canonical"
    CANDIDATE = "candidate"


class ResearchTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


class ToolReadStatus(str, Enum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


class QuarantineReason(str, Enum):
    FUTURE_INFORMATION = "future_information"
    AVAILABLE_AT_UNPROVEN = "available_at_unproven"
    SOURCE_POLICY = "source_policy"
    SCHEMA_INVALID = "schema_invalid"
    HASH_MISMATCH = "hash_mismatch"
    DEFERRED_SOURCE = "deferred_source"


class GovernanceModel(BaseModel):
    """Strict, frozen base for every governance-domain value object."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )
    schema_name: ClassVar[str] = "governance-object"

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def calculate_canonical_hash(
        self,
        *,
        hash_field: str = "canonical_hash",
    ) -> str:
        excluded = set(type(self).model_computed_fields)
        excluded.add(hash_field)
        payload = self.model_dump(
            mode="python",
            by_alias=True,
            exclude=excluded,
        )
        schema_version = getattr(self, "schema_version", MODEL_SCHEMA_VERSION)
        return canonical_sha256(
            payload,
            schema_name=self.schema_name,
            schema_version=str(schema_version),
        )

    @classmethod
    def model_validate_with_hash(
        cls,
        payload: Mapping[str, Any],
        *,
        hash_field: str = "canonical_hash",
    ) -> Self:
        """Validate normalized fields before deriving and revalidating the object hash."""

        if hash_field not in cls.model_fields:
            raise ValueError(f"{cls.__name__} has no {hash_field!r} field")
        provisional_payload = dict(payload)
        provisional_payload[hash_field] = "0" * 64
        provisional = cls.model_validate(provisional_payload, strict=True)
        final_payload = provisional.model_dump(
            mode="python",
            by_alias=True,
            exclude=set(cls.model_computed_fields),
        )
        final_payload[hash_field] = provisional.calculate_canonical_hash(
            hash_field=hash_field
        )
        return cls.model_validate(final_payload, strict=True)

    @classmethod
    def model_validate_canonical_json(
        cls,
        data: bytes | bytearray | memoryview | str,
    ) -> Self:
        load_canonical_json(data)
        return cls.model_validate_json(data, strict=True)


def _require_prefix(value: str, prefix: str, field_name: str) -> None:
    if not value.startswith(f"{prefix}:") or len(value) == len(prefix) + 1:
        raise ValueError(f"{field_name} must use the {prefix}: namespace")


def _check_interval(
    start: datetime | None,
    end: datetime | None,
    *,
    name: str,
) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError(f"{name} end must not precede start")


def _check_query_time(
    state_at: datetime,
    known_at: datetime,
    perspective: GovernancePerspective,
    future_knowledge_used: bool,
) -> None:
    if known_at < state_at:
        raise ValueError("known_at must not precede state_at in governance v1")
    if perspective == GovernancePerspective.STRICT and known_at != state_at:
        raise ValueError("strict perspective requires known_at == state_at")
    expected_future = (
        perspective == GovernancePerspective.RECONSTRUCTED and known_at > state_at
    )
    if future_knowledge_used != expected_future:
        raise ValueError("future_knowledge_used does not match the bitemporal perspective")


ClaimScalar = UtcDateTime | date | CanonicalDecimal | str | int | bool | None


class GovernanceExtractionRun(GovernanceModel):
    schema_name: ClassVar[str] = "governance-extraction-run"
    kind: Literal["governance_extraction_run"] = "governance_extraction_run"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    extraction_run_id: NonEmptyStr
    evidence_manifest_id: NonEmptyStr
    raw_snapshot_id: NonEmptyStr
    content_hash: Sha256Hex
    question_ids: SortedUniqueQuestionIds
    extractor_kind: ExtractorKind
    extractor_name: NonEmptyStr
    extractor_version: NonEmptyStr
    target_schema_version: NonEmptyStr
    started_at: UtcDateTime
    completed_at: UtcDateTime | None = None
    output_claim_ids: SortedUniqueStrings = ()
    validation_result_ids: SortedUniqueStrings = ()
    error_code: NonEmptyStr | None = None
    error_summary: NonEmptyStr | None = None
    run_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        _require_prefix(self.extraction_run_id, "govxrun", "extraction_run_id")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if (self.error_code is None) != (self.error_summary is None):
            raise ValueError("error_code and error_summary must be supplied together")
        return self


class GovernanceEvidenceSpan(GovernanceModel):
    schema_name: ClassVar[str] = "governance-evidence-span"
    kind: Literal["governance_evidence_span"] = "governance_evidence_span"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    evidence_span_id: NonEmptyStr
    raw_snapshot_id: NonEmptyStr
    content_hash: Sha256Hex
    derived_artifact_id: NonEmptyStr | None = None
    derived_artifact_hash: Sha256Hex | None = None
    page: int | None = Field(default=None, ge=1)
    table_id: NonEmptyStr | None = None
    row_label: NonEmptyStr | None = None
    column_label: NonEmptyStr | None = None
    paragraph_id: NonEmptyStr | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    excerpt_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_locator(self) -> Self:
        _require_prefix(self.evidence_span_id, "govspan", "evidence_span_id")
        if (self.derived_artifact_id is None) != (self.derived_artifact_hash is None):
            raise ValueError("derived artifact ID and hash must be supplied together")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be supplied together")
        if self.char_start is not None and self.char_end is not None:
            if self.char_end <= self.char_start:
                raise ValueError("char_end must be greater than char_start")
        if (self.row_label is not None or self.column_label is not None) and self.table_id is None:
            raise ValueError("table row/column locators require table_id")
        if not any(
            (
                self.page is not None,
                self.table_id is not None,
                self.paragraph_id is not None,
                self.char_start is not None,
            )
        ):
            raise ValueError("an evidence span requires a stable field-level locator")
        return self

    @property
    def locator_complete(self) -> bool:
        return True


class GovernanceClaim(GovernanceModel):
    schema_name: ClassVar[str] = "governance-claim"
    kind: Literal["governance_claim"] = "governance_claim"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    claim_id: NonEmptyStr
    extraction_run_id: NonEmptyStr
    company_id: NonEmptyStr
    question_id: QuestionId
    subject_type: NonEmptyStr
    subject_id: NonEmptyStr
    predicate: NonEmptyStr
    object_type: ClaimObjectType
    object_value: ClaimScalar
    unit: NonEmptyStr | None = None
    currency: NonEmptyStr | None = None
    record_type: NonEmptyStr
    target_record_id: NonEmptyStr | None = None
    reference_at: UtcDateTime | None = None
    effective_at: UtcDateTime | None = None
    valid_from: UtcDateTime | None = None
    valid_to: UtcDateTime | None = None
    announced_at: UtcDateTime
    available_at: UtcDateTime
    retrieved_at: UtcDateTime
    original_timezone: NonEmptyStr = "Asia/Shanghai"
    time_precision: TimePrecision
    source_role: SourceRole
    evidence_manifest_id: NonEmptyStr
    raw_snapshot_id: NonEmptyStr
    content_hash: Sha256Hex
    evidence_span_id: NonEmptyStr
    upstream_material_id: NonEmptyStr
    independence_group: NonEmptyStr
    extractor_kind: ExtractorKind
    extractor_version: NonEmptyStr
    extraction_status: ExtractionStatus
    verification_status: VerificationStatus
    review_status: ReviewStatus
    raw_hash_verified: bool
    lineage_complete: bool
    evidence_locator_complete: bool
    has_unresolved_conflict: bool = False
    supersedes_claim_id: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @field_validator("object_value", mode="before")
    @classmethod
    def parse_typed_object_value(cls, value: Any, info: ValidationInfo) -> Any:
        object_type = info.data.get("object_type")
        if value is None:
            return value
        if object_type == ClaimObjectType.DECIMAL and isinstance(value, str):
            return _decimal_input(value, info)
        if object_type == ClaimObjectType.DATE and isinstance(value, str):
            return date.fromisoformat(value)
        if object_type == ClaimObjectType.DATETIME and isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        _require_prefix(self.claim_id, "govclaim", "claim_id")
        _require_prefix(self.extraction_run_id, "govxrun", "extraction_run_id")
        _require_prefix(self.evidence_span_id, "govspan", "evidence_span_id")
        _check_interval(self.valid_from, self.valid_to, name="claim validity")
        if self.available_at < self.announced_at:
            raise ValueError("available_at must not precede announced_at")
        if self.retrieved_at < self.available_at:
            raise ValueError("retrieved_at must not precede available_at")
        expected_types: dict[ClaimObjectType, tuple[type[Any], ...]] = {
            ClaimObjectType.TEXT: (str,),
            ClaimObjectType.IDENTIFIER: (str,),
            ClaimObjectType.INTEGER: (int,),
            ClaimObjectType.DECIMAL: (Decimal,),
            ClaimObjectType.BOOLEAN: (bool,),
            ClaimObjectType.DATE: (date,),
            ClaimObjectType.DATETIME: (datetime,),
            ClaimObjectType.UNKNOWN: (type(None),),
        }
        expected = expected_types[self.object_type]
        if not isinstance(self.object_value, expected):
            raise ValueError("object_value does not match object_type")
        if self.object_type == ClaimObjectType.INTEGER and isinstance(self.object_value, bool):
            raise ValueError("boolean is not an integer claim value")
        if self.object_type == ClaimObjectType.DATE and isinstance(self.object_value, datetime):
            raise ValueError("datetime is not a date-precision claim value")
        if self.object_type == ClaimObjectType.DATETIME:
            value = self.object_value
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("datetime claim values must be timezone-aware")
        return self

    @property
    def canonical_eligible(self) -> bool:
        """A claim alone can never prove canonical admission.

        Call ``admission.canonical_eligible`` with the independently loaded
        evidence span and validator results.  Keeping this compatibility
        property fail-closed prevents older callers from trusting the claim's
        self-reported integrity flags.
        """

        return False


class ValidationCheck(GovernanceModel):
    schema_name: ClassVar[str] = "governance-validation-check"
    kind: Literal["validation_check"] = "validation_check"
    check_code: NonEmptyStr
    passed: bool
    detail: NonEmptyStr | None = None
    evidence_span_ids: SortedUniqueStrings = ()


class ValidationResult(GovernanceModel):
    schema_name: ClassVar[str] = "governance-validation-result"
    kind: Literal["validation_result"] = "validation_result"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    validation_result_id: NonEmptyStr
    claim_id: NonEmptyStr
    validator_name: NonEmptyStr
    validator_version: NonEmptyStr
    verification_status: VerificationStatus
    checks: tuple[ValidationCheck, ...]
    validated_at: UtcDateTime
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        _require_prefix(
            self.validation_result_id, "govvalidation", "validation_result_id"
        )
        _require_prefix(self.claim_id, "govclaim", "claim_id")
        if not self.checks:
            raise ValueError("validation results require at least one check")
        if self.verification_status == VerificationStatus.PASSED and not all(
            check.passed for check in self.checks
        ):
            raise ValueError("passed validation results cannot contain failed checks")
        if self.verification_status == VerificationStatus.FAILED and all(
            check.passed for check in self.checks
        ):
            raise ValueError("failed validation results require a failed check")
        return self


class ReviewDecision(GovernanceModel):
    schema_name: ClassVar[str] = "governance-review-decision"
    kind: Literal["review_decision"] = "review_decision"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    review_decision_id: NonEmptyStr
    candidate_claim_id: NonEmptyStr
    decision: DecisionValue
    decided_by: NonEmptyStr
    rationale: NonEmptyStr
    evidence_span_ids: SortedUniqueStrings
    decided_at: UtcDateTime
    available_at: UtcDateTime
    supersedes_decision_id: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        _require_prefix(self.review_decision_id, "govreview", "review_decision_id")
        _require_prefix(self.candidate_claim_id, "govclaim", "candidate_claim_id")
        if self.available_at < self.decided_at:
            raise ValueError("review available_at must not precede decided_at")
        if not self.evidence_span_ids:
            raise ValueError("review decisions require evidence")
        if self.supersedes_decision_id == self.review_decision_id:
            raise ValueError("a review decision cannot supersede itself")
        return self


class GapRecord(GovernanceModel):
    schema_name: ClassVar[str] = "governance-gap"
    kind: Literal["gap_record"] = "gap_record"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    gap_id: NonEmptyStr
    company_id: NonEmptyStr
    question_id: QuestionId
    status: RecordResolutionStatus
    reason_code: NonEmptyStr
    detail: NonEmptyStr
    state_start: UtcDateTime | None = None
    state_end: UtcDateTime | None = None
    coverage_entry_ids: SortedUniqueStrings = ()
    supporting_claim_ids: SortedUniqueStrings = ()
    available_at: UtcDateTime
    supersedes_gap_id: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_gap(self) -> Self:
        _require_prefix(self.gap_id, "govgap", "gap_id")
        _check_interval(self.state_start, self.state_end, name="gap interval")
        if self.supersedes_gap_id == self.gap_id:
            raise ValueError("a gap cannot supersede itself")
        return self


class ConflictRecord(GovernanceModel):
    schema_name: ClassVar[str] = "governance-conflict"
    kind: Literal["conflict_record"] = "conflict_record"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    conflict_id: NonEmptyStr
    company_id: NonEmptyStr
    question_id: QuestionId
    subject_id: NonEmptyStr
    predicate: NonEmptyStr
    claim_ids: SortedUniqueStrings
    status: RecordResolutionStatus = RecordResolutionStatus.ACTIVE
    resolution_claim_id: NonEmptyStr | None = None
    reason: NonEmptyStr
    available_at: UtcDateTime
    supersedes_conflict_id: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_conflict(self) -> Self:
        _require_prefix(self.conflict_id, "govconflict", "conflict_id")
        if len(self.claim_ids) < 2:
            raise ValueError("conflicts require at least two claims")
        for claim_id in self.claim_ids:
            _require_prefix(claim_id, "govclaim", "claim_ids")
        if self.status == RecordResolutionStatus.RESOLVED and self.resolution_claim_id is None:
            raise ValueError("resolved conflicts require resolution_claim_id")
        if self.status == RecordResolutionStatus.ACTIVE and self.resolution_claim_id is not None:
            raise ValueError("active conflicts cannot select a precise resolution")
        if self.supersedes_conflict_id == self.conflict_id:
            raise ValueError("a conflict cannot supersede itself")
        return self


class GovernanceRecordBase(GovernanceModel):
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    record_id: NonEmptyStr
    company_id: NonEmptyStr
    question_id: QuestionId
    claim_ids: SortedUniqueStrings
    evidence_span_ids: SortedUniqueStrings
    source_event_ids: SortedUniqueStrings = ()
    reference_at: UtcDateTime | None = None
    effective_at: UtcDateTime | None = None
    valid_from: UtcDateTime | None = None
    valid_to: UtcDateTime | None = None
    available_at: UtcDateTime
    available_time_precision: TimePrecision = TimePrecision.DATETIME
    completeness_status: CompletenessStatus
    supersedes_record_id: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_common_record(self) -> Self:
        _require_prefix(self.record_id, "govrec", "record_id")
        if not self.claim_ids or not self.evidence_span_ids:
            raise ValueError("typed records require claim and evidence lineage")
        for claim_id in self.claim_ids:
            _require_prefix(claim_id, "govclaim", "claim_ids")
        for span_id in self.evidence_span_ids:
            _require_prefix(span_id, "govspan", "evidence_span_ids")
        _check_interval(self.valid_from, self.valid_to, name="record validity")
        if (
            self.available_time_precision == TimePrecision.DATE
            and self.available_at.astimezone(SHANGHAI_TIMEZONE).timetz().replace(
                tzinfo=None
            )
            != time.min
        ):
            raise ValueError(
                "date-precision available_at must use the exclusive next-midnight "
                "Asia/Shanghai boundary"
            )
        if self.supersedes_record_id == self.record_id:
            raise ValueError("a record cannot supersede itself")
        return self


class CorrectionRecord(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-correction-record"
    kind: Literal["correction_record"] = "correction_record"
    corrected_claim_id: NonEmptyStr | None = None
    corrected_record_id: NonEmptyStr | None = None
    old_value: NonEmptyStr
    new_value: NonEmptyStr
    reason: NonEmptyStr

    @model_validator(mode="after")
    def validate_correction(self) -> Self:
        if (self.corrected_claim_id is None) == (self.corrected_record_id is None):
            raise ValueError("a correction must target exactly one claim or record")
        if self.corrected_claim_id is not None:
            _require_prefix(self.corrected_claim_id, "govclaim", "corrected_claim_id")
        if self.corrected_record_id is not None:
            _require_prefix(self.corrected_record_id, "govrec", "corrected_record_id")
        if self.old_value == self.new_value:
            raise ValueError("a correction must change the disclosed value")
        return self


class GovernancePerson(GovernanceModel):
    schema_name: ClassVar[str] = "governance-person"
    kind: Literal["governance_person"] = "governance_person"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    person_id: NonEmptyStr
    company_id: NonEmptyStr
    stable_local_key: NonEmptyStr
    canonical_name: NonEmptyStr
    source_claim_ids: SortedUniqueStrings
    available_at: UtcDateTime
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_person_namespace(self) -> Self:
        expected = f"govp:{self.company_id}:"
        if not self.person_id.startswith(expected) or self.person_id == expected:
            raise ValueError("person_id must be namespaced inside its company_id")
        if any(char.isspace() for char in self.stable_local_key):
            raise ValueError("stable_local_key must not contain whitespace")
        for claim_id in self.source_claim_ids:
            _require_prefix(claim_id, "govclaim", "source_claim_ids")
        if not self.source_claim_ids:
            raise ValueError("governance persons require source claim lineage")
        return self


class PersonAlias(GovernanceModel):
    schema_name: ClassVar[str] = "governance-person-alias"
    kind: Literal["person_alias"] = "person_alias"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    alias_id: NonEmptyStr
    company_id: NonEmptyStr
    person_id: NonEmptyStr
    alias: NonEmptyStr
    alias_type: NonEmptyStr
    raw_snapshot_id: NonEmptyStr
    evidence_span_id: NonEmptyStr
    extractor_version: NonEmptyStr
    available_at: UtcDateTime
    supersedes_alias_id: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_alias(self) -> Self:
        _require_prefix(self.alias_id, "govalias", "alias_id")
        if not self.person_id.startswith(f"govp:{self.company_id}:"):
            raise ValueError("person alias must stay inside the company namespace")
        _require_prefix(self.evidence_span_id, "govspan", "evidence_span_id")
        if self.supersedes_alias_id == self.alias_id:
            raise ValueError("an alias cannot supersede itself")
        return self


class BiographyClaim(GovernanceModel):
    schema_name: ClassVar[str] = "governance-biography-claim"
    kind: Literal["biography_claim"] = "biography_claim"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    biography_claim_id: NonEmptyStr
    company_id: NonEmptyStr
    person_id: NonEmptyStr
    claim_id: NonEmptyStr
    biography_text: NonEmptyStr
    period_start: UtcDateTime | None = None
    period_end: UtcDateTime | None = None
    source_role: SourceRole
    raw_snapshot_id: NonEmptyStr
    evidence_span_id: NonEmptyStr
    available_at: UtcDateTime
    extractor_version: NonEmptyStr
    supersedes_biography_claim_id: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_biography(self) -> Self:
        _require_prefix(
            self.biography_claim_id, "govbio", "biography_claim_id"
        )
        if not self.person_id.startswith(f"govp:{self.company_id}:"):
            raise ValueError("biography claim must stay inside the company namespace")
        _require_prefix(self.claim_id, "govclaim", "claim_id")
        _require_prefix(self.evidence_span_id, "govspan", "evidence_span_id")
        _check_interval(self.period_start, self.period_end, name="biography period")
        if self.source_role not in {
            SourceRole.OFFICIAL_DISCLOSURE,
            SourceRole.REGULATOR_EXCHANGE,
        }:
            raise ValueError("authoritative biography claims require a formal source")
        if self.supersedes_biography_claim_id == self.biography_claim_id:
            raise ValueError("a biography claim cannot supersede itself")
        return self


class PersonLinkCandidate(GovernanceModel):
    schema_name: ClassVar[str] = "governance-person-link-candidate"
    kind: Literal["person_link_candidate"] = "person_link_candidate"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    person_link_candidate_id: NonEmptyStr
    company_ids: OrderedUniqueStrings
    person_ids: OrderedUniqueStrings
    basis: NonEmptyStr
    producer: NonEmptyStr
    extractor_kind: ExtractorKind
    evidence_span_ids: SortedUniqueStrings
    available_at: UtcDateTime
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        _require_prefix(
            self.person_link_candidate_id,
            "govlinkcandidate",
            "person_link_candidate_id",
        )
        if len(self.company_ids) < 2 or len(self.company_ids) != len(self.person_ids):
            raise ValueError("cross-company link candidates require aligned companies/persons")
        for company_id, person_id in zip(self.company_ids, self.person_ids, strict=True):
            if not person_id.startswith(f"govp:{company_id}:"):
                raise ValueError("linked person_id does not match its company namespace")
        if not self.evidence_span_ids:
            raise ValueError("person link candidates require evidence")
        return self


class PersonLinkDecision(GovernanceModel):
    schema_name: ClassVar[str] = "governance-person-link-decision"
    kind: Literal["person_link_decision"] = "person_link_decision"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    person_link_decision_id: NonEmptyStr
    person_link_candidate_id: NonEmptyStr
    company_ids: OrderedUniqueStrings
    person_ids: OrderedUniqueStrings
    decision: DecisionValue
    decision_source: NonEmptyStr
    producer: NonEmptyStr
    rationale: NonEmptyStr
    evidence_span_ids: SortedUniqueStrings
    decided_at: UtcDateTime
    available_at: UtcDateTime
    supersedes_decision_id: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_link_decision(self) -> Self:
        _require_prefix(
            self.person_link_decision_id,
            "govlinkdecision",
            "person_link_decision_id",
        )
        _require_prefix(
            self.person_link_candidate_id,
            "govlinkcandidate",
            "person_link_candidate_id",
        )
        if len(self.company_ids) < 2 or len(self.company_ids) != len(self.person_ids):
            raise ValueError("link decisions require aligned companies/persons")
        for company_id, person_id in zip(self.company_ids, self.person_ids, strict=True):
            if not person_id.startswith(f"govp:{company_id}:"):
                raise ValueError("linked person_id does not match its company namespace")
        if self.available_at < self.decided_at:
            raise ValueError("link decision available_at must not precede decided_at")
        if not self.evidence_span_ids:
            raise ValueError("person link decisions require evidence")
        if self.supersedes_decision_id == self.person_link_decision_id:
            raise ValueError("a link decision cannot supersede itself")
        return self


class RosterSnapshot(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-roster-snapshot"
    kind: Literal["roster_snapshot"] = "roster_snapshot"
    body_type: NonEmptyStr
    is_complete: bool
    member_ids: SortedUniqueStrings
    source_manifest_id: NonEmptyStr
    completeness_evidence_span_ids: SortedUniqueStrings = ()

    @model_validator(mode="after")
    def validate_roster(self) -> Self:
        for person_id in self.member_ids:
            if not person_id.startswith(f"govp:{self.company_id}:"):
                raise ValueError("roster members must use company-local person IDs")
        if self.is_complete and not self.completeness_evidence_span_ids:
            raise ValueError("complete rosters require disclosure-scope evidence")
        if self.is_complete != (
            self.completeness_status == CompletenessStatus.COMPLETE
        ):
            raise ValueError("roster is_complete must match completeness_status")
        return self


class RoleTenure(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-role-tenure"
    kind: Literal["role_tenure"] = "role_tenure"
    person_id: NonEmptyStr
    role_code: NonEmptyStr
    original_role_text: NonEmptyStr
    role_scope: NonEmptyStr
    acting: bool = False
    appointment_event_id: NonEmptyStr | None = None
    termination_event_id: NonEmptyStr | None = None
    appointment_evidence_span_id: NonEmptyStr
    termination_evidence_span_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_tenure(self) -> Self:
        if not self.person_id.startswith(f"govp:{self.company_id}:"):
            raise ValueError("role tenure must use a company-local person ID")
        if self.valid_from is None:
            raise ValueError("role tenure requires valid_from")
        _require_prefix(
            self.appointment_evidence_span_id,
            "govspan",
            "appointment_evidence_span_id",
        )
        if (self.valid_to is None) != (self.termination_evidence_span_id is None):
            raise ValueError("a closed tenure requires explicit termination evidence")
        return self


class OwnershipPosition(GovernanceModel):
    schema_name: ClassVar[str] = "governance-ownership-position"
    kind: Literal["ownership_position"] = "ownership_position"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    position_id: NonEmptyStr
    company_id: NonEmptyStr
    holder_entity_id: NonEmptyStr
    holder_name: NonEmptyStr
    share_count: CanonicalDecimal | None = None
    ratio: CanonicalDecimal | None = None
    ratio_basis: CanonicalDecimal | None = None
    share_class: NonEmptyStr
    capital_basis: NonEmptyStr | None = None
    completeness_status: CompletenessStatus
    claim_ids: SortedUniqueStrings
    evidence_span_ids: SortedUniqueStrings
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_position(self) -> Self:
        _require_prefix(self.position_id, "govposition", "position_id")
        if self.share_count is None and self.ratio is None:
            raise ValueError("ownership positions require a share count or disclosed ratio")
        for value in (self.share_count, self.ratio, self.ratio_basis):
            if value is not None and value < 0:
                raise ValueError("ownership quantities cannot be negative")
        if self.ratio is not None and self.ratio_basis is None:
            raise ValueError("ownership ratios require an explicit calculation basis")
        return self


class OwnershipSnapshot(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-ownership-snapshot"
    kind: Literal["ownership_snapshot"] = "ownership_snapshot"
    positions: tuple[OwnershipPosition, ...]
    total_share_basis: CanonicalDecimal | None = None
    capital_basis: NonEmptyStr | None = None
    is_complete: bool
    source_manifest_id: NonEmptyStr

    @model_validator(mode="after")
    def validate_ownership_snapshot(self) -> Self:
        if self.reference_at is None:
            raise ValueError("ownership snapshots require reference_at")
        if not self.positions:
            raise ValueError("ownership snapshots require at least one position")
        position_ids = [position.position_id for position in self.positions]
        if position_ids != sorted(position_ids) or len(position_ids) != len(set(position_ids)):
            raise ValueError("ownership positions must be unique and stably sorted")
        if any(position.company_id != self.company_id for position in self.positions):
            raise ValueError("ownership positions cannot cross company namespaces")
        if self.is_complete and (self.total_share_basis is None or self.capital_basis is None):
            raise ValueError("complete ownership snapshots require basis and capital scope")
        if self.is_complete != (
            self.completeness_status == CompletenessStatus.COMPLETE
        ):
            raise ValueError("ownership is_complete must match completeness_status")
        return self


class ControlRelation(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-control-relation"
    kind: Literal["control_relation"] = "control_relation"
    controller_entity_id: NonEmptyStr
    controlled_entity_id: NonEmptyStr
    relation_type: NonEmptyStr
    direction: DirectionKind
    chain_path: OrderedUniqueStrings

    @model_validator(mode="after")
    def validate_control(self) -> Self:
        if self.controller_entity_id == self.controlled_entity_id:
            raise ValueError("an entity cannot control itself")
        if len(self.chain_path) < 2:
            raise ValueError("control relations require a reproducible chain path")
        if self.chain_path[0] != self.controller_entity_id:
            raise ValueError("control chain must start at the controller")
        if self.chain_path[-1] != self.controlled_entity_id:
            raise ValueError("control chain must end at the controlled entity")
        if self.direction == DirectionKind.DIRECT and len(self.chain_path) != 2:
            raise ValueError("direct control must not contain intermediate nodes")
        return self


class PledgePositionSnapshot(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-pledge-position-snapshot"
    kind: Literal["pledge_position_snapshot"] = "pledge_position_snapshot"
    pledgor_entity_id: NonEmptyStr
    pledgee_entity_id: NonEmptyStr | None = None
    pledged_shares: CanonicalDecimal | None = None
    pledged_ratio: CanonicalDecimal | None = None
    ratio_basis: CanonicalDecimal | None = None
    capital_basis: NonEmptyStr | None = None
    is_complete: bool

    @model_validator(mode="after")
    def validate_pledge(self) -> Self:
        if self.reference_at is None:
            raise ValueError("pledge snapshots require reference_at")
        for value in (self.pledged_shares, self.pledged_ratio, self.ratio_basis):
            if value is not None and value < 0:
                raise ValueError("pledge quantities cannot be negative")
        if self.pledged_ratio is not None and self.ratio_basis is None:
            raise ValueError("pledge ratios require an explicit calculation basis")
        if self.is_complete and (
            self.pledged_shares is None
            or self.ratio_basis is None
            or self.capital_basis is None
        ):
            raise ValueError("complete pledge snapshots require shares, basis and capital scope")
        if self.is_complete != (
            self.completeness_status == CompletenessStatus.COMPLETE
        ):
            raise ValueError("pledge is_complete must match completeness_status")
        return self


class CompensationRecord(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-compensation-record"
    kind: Literal["compensation_record"] = "compensation_record"
    person_id: NonEmptyStr
    fiscal_year: int = Field(ge=1900, le=9999)
    cash_compensation: CanonicalDecimal | None = None
    equity_component: CanonicalDecimal | None = None
    currency: NonEmptyStr
    scope: NonEmptyStr

    @model_validator(mode="after")
    def validate_compensation(self) -> Self:
        if not self.person_id.startswith(f"govp:{self.company_id}:"):
            raise ValueError("compensation must use a company-local person ID")
        if self.cash_compensation is None and self.equity_component is None:
            raise ValueError("compensation requires a disclosed cash or equity component")
        for value in (self.cash_compensation, self.equity_component):
            if value is not None and value < 0:
                raise ValueError("compensation components cannot be negative")
        return self


class RelatedPartyRelation(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-related-party-relation"
    kind: Literal["related_party_relation"] = "related_party_relation"
    related_entity_id: NonEmptyStr
    relation_type: NonEmptyStr
    basis: NonEmptyStr


class RelatedPartyTransaction(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-related-party-transaction"
    kind: Literal["related_party_transaction"] = "related_party_transaction"
    counterparty_entity_id: NonEmptyStr
    transaction_type: NonEmptyStr
    amount: CanonicalDecimal | None = None
    currency: NonEmptyStr | None = None
    approval_status: NonEmptyStr
    fiscal_period_start: date
    fiscal_period_end: date

    @model_validator(mode="after")
    def validate_transaction(self) -> Self:
        if self.fiscal_period_end < self.fiscal_period_start:
            raise ValueError("transaction fiscal period is reversed")
        if (self.amount is None) != (self.currency is None):
            raise ValueError("transaction amount and currency must be supplied together")
        if self.amount is not None and self.amount < 0:
            raise ValueError("transaction amount cannot be negative")
        return self


class IncentivePlan(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-incentive-plan"
    kind: Literal["incentive_plan"] = "incentive_plan"
    plan_id: NonEmptyStr
    instrument: NonEmptyStr
    grant_pool: CanonicalDecimal | None = None
    status: NonEmptyStr
    dilution_basis: CanonicalDecimal | None = None

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        _require_prefix(self.plan_id, "govplan", "plan_id")
        for value in (self.grant_pool, self.dilution_basis):
            if value is not None and value < 0:
                raise ValueError("incentive quantities cannot be negative")
        return self


class IncentiveGrant(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-incentive-grant"
    kind: Literal["incentive_grant"] = "incentive_grant"
    plan_id: NonEmptyStr
    recipient_scope: NonEmptyStr
    recipient_person_ids: SortedUniqueStrings = ()
    quantity: CanonicalDecimal
    price: CanonicalDecimal | None = None
    currency: NonEmptyStr | None = None
    grant_at: UtcDateTime
    vesting_start_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def validate_grant(self) -> Self:
        _require_prefix(self.plan_id, "govplan", "plan_id")
        if self.quantity < 0:
            raise ValueError("grant quantity cannot be negative")
        if self.price is not None and self.price < 0:
            raise ValueError("grant price cannot be negative")
        if (self.price is None) != (self.currency is None):
            raise ValueError("grant price and currency must be supplied together")
        if self.vesting_start_at is not None and self.vesting_start_at < self.grant_at:
            raise ValueError("vesting cannot start before grant")
        for person_id in self.recipient_person_ids:
            if not person_id.startswith(f"govp:{self.company_id}:"):
                raise ValueError("grant recipients must use company-local person IDs")
        return self


# The implementation document uses both names; they intentionally denote one schema.
Grant = IncentiveGrant


class VestingCondition(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-vesting-condition"
    kind: Literal["vesting_condition"] = "vesting_condition"
    plan_id: NonEmptyStr
    period_label: NonEmptyStr
    metric: NonEmptyStr
    threshold: NonEmptyStr
    actual_disclosure: NonEmptyStr | None = None
    status: NonEmptyStr

    @model_validator(mode="after")
    def validate_vesting(self) -> Self:
        _require_prefix(self.plan_id, "govplan", "plan_id")
        return self


class AuditorEngagement(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-auditor-engagement"
    kind: Literal["auditor_engagement"] = "auditor_engagement"
    audit_firm_entity_id: NonEmptyStr
    signing_auditors: SortedUniqueStrings = ()
    fiscal_period_start: date
    fiscal_period_end: date
    fee: CanonicalDecimal | None = None
    currency: NonEmptyStr | None = None
    change_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_engagement(self) -> Self:
        if self.fiscal_period_end < self.fiscal_period_start:
            raise ValueError("auditor fiscal period is reversed")
        if (self.fee is None) != (self.currency is None):
            raise ValueError("audit fee and currency must be supplied together")
        if self.fee is not None and self.fee < 0:
            raise ValueError("audit fee cannot be negative")
        return self


class AuditOpinionRecord(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-audit-opinion-record"
    kind: Literal["audit_opinion_record"] = "audit_opinion_record"
    report_period_start: date
    report_period_end: date
    opinion_type: NonEmptyStr
    emphasis_or_key_matter: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_audit_opinion(self) -> Self:
        if self.report_period_end < self.report_period_start:
            raise ValueError("audit opinion period is reversed")
        return self


class InternalControlRecord(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-internal-control-record"
    kind: Literal["internal_control_record"] = "internal_control_record"
    report_period_start: date
    report_period_end: date
    opinion: NonEmptyStr
    defect_category: NonEmptyStr | None = None
    rectification_status: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_internal_control(self) -> Self:
        if self.report_period_end < self.report_period_start:
            raise ValueError("internal-control period is reversed")
        return self


class RegulatoryMatter(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-regulatory-matter"
    kind: Literal["regulatory_matter"] = "regulatory_matter"
    authority: NonEmptyStr
    measure_type: NonEmptyStr
    subject_ids: SortedUniqueStrings
    decision_date: date
    disclosed_status: NonEmptyStr

    @model_validator(mode="after")
    def validate_regulatory(self) -> Self:
        if not self.subject_ids:
            raise ValueError("regulatory matters require at least one subject")
        return self


class InquiryRecord(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-inquiry-record"
    kind: Literal["inquiry_record"] = "inquiry_record"
    authority: NonEmptyStr
    question_categories: SortedUniqueStrings
    issued_at: UtcDateTime
    responded_at: UtcDateTime | None = None
    disclosed_status: NonEmptyStr

    @model_validator(mode="after")
    def validate_inquiry(self) -> Self:
        if not self.question_categories:
            raise ValueError("inquiries require at least one question category")
        if self.responded_at is not None and self.responded_at < self.issued_at:
            raise ValueError("inquiry response cannot precede issuance")
        return self


class LitigationMatter(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-litigation-matter"
    kind: Literal["litigation_matter"] = "litigation_matter"
    disclosed_party_ids: SortedUniqueStrings
    amount: CanonicalDecimal | None = None
    currency: NonEmptyStr | None = None
    disclosed_stage: NonEmptyStr
    materiality_basis: NonEmptyStr
    source_scope: Literal["formal_disclosures_only"] = "formal_disclosures_only"

    @model_validator(mode="after")
    def validate_litigation(self) -> Self:
        if not self.disclosed_party_ids:
            raise ValueError("litigation matters require disclosed parties")
        if (self.amount is None) != (self.currency is None):
            raise ValueError("litigation amount and currency must be supplied together")
        if self.amount is not None and self.amount < 0:
            raise ValueError("litigation amount cannot be negative")
        return self


class CommitmentRecord(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-commitment-record"
    kind: Literal["commitment_record"] = "commitment_record"
    promisor_entity_id: NonEmptyStr
    obligation: NonEmptyStr
    deadline: UtcDateTime | None = None
    disclosed_fulfillment_status: NonEmptyStr


class GovernancePolicyVersion(GovernanceRecordBase):
    schema_name: ClassVar[str] = "governance-policy-version"
    kind: Literal["governance_policy_version"] = "governance_policy_version"
    policy_id: NonEmptyStr
    policy_type: NonEmptyStr
    effective_date: date
    version_hash: Sha256Hex
    supersedes_policy_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        _require_prefix(self.policy_id, "govpolicy", "policy_id")
        if self.supersedes_policy_id == self.policy_id:
            raise ValueError("a policy version cannot supersede itself")
        return self


GovernanceRecord = Annotated[
    RosterSnapshot
    | RoleTenure
    | OwnershipPosition
    | OwnershipSnapshot
    | ControlRelation
    | PledgePositionSnapshot
    | CompensationRecord
    | RelatedPartyRelation
    | RelatedPartyTransaction
    | IncentivePlan
    | IncentiveGrant
    | VestingCondition
    | AuditorEngagement
    | AuditOpinionRecord
    | InternalControlRecord
    | RegulatoryMatter
    | InquiryRecord
    | LitigationMatter
    | CommitmentRecord
    | GovernancePolicyVersion
    | CorrectionRecord,
    Field(discriminator="kind"),
]
GOVERNANCE_RECORD_ADAPTER = TypeAdapter(GovernanceRecord)


class SnapshotAnchorLink(GovernanceModel):
    schema_name: ClassVar[str] = "governance-snapshot-anchor-link"
    kind: Literal["snapshot_anchor_link"] = "snapshot_anchor_link"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    anchor_link_id: NonEmptyStr
    governance_snapshot_id: NonEmptyStr
    question_id: QuestionId
    state_kind: NonEmptyStr
    anchor_record_id: NonEmptyStr
    reference_at: UtcDateTime
    available_at: UtcDateTime
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_anchor_link(self) -> Self:
        _require_prefix(self.anchor_link_id, "govanchor", "anchor_link_id")
        _require_prefix(
            self.governance_snapshot_id, "govsnapshot", "governance_snapshot_id"
        )
        _require_prefix(self.anchor_record_id, "govrec", "anchor_record_id")
        return self


class SnapshotDeltaLink(GovernanceModel):
    schema_name: ClassVar[str] = "governance-snapshot-delta-link"
    kind: Literal["snapshot_delta_link"] = "snapshot_delta_link"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    delta_link_id: NonEmptyStr
    governance_snapshot_id: NonEmptyStr
    question_id: QuestionId
    delta_record_id: NonEmptyStr
    disposition: DeltaDisposition
    sequence: int = Field(ge=1)
    effective_at: UtcDateTime
    available_at: UtcDateTime
    exclusion_reason: NonEmptyStr | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_delta_link(self) -> Self:
        _require_prefix(self.delta_link_id, "govdelta", "delta_link_id")
        _require_prefix(
            self.governance_snapshot_id, "govsnapshot", "governance_snapshot_id"
        )
        _require_prefix(self.delta_record_id, "govrec", "delta_record_id")
        if self.disposition == DeltaDisposition.APPLIED and self.exclusion_reason is not None:
            raise ValueError("applied deltas cannot have an exclusion reason")
        if self.disposition == DeltaDisposition.EXCLUDED and self.exclusion_reason is None:
            raise ValueError("excluded deltas require a stable reason")
        return self


class SnapshotRecordLink(GovernanceModel):
    schema_name: ClassVar[str] = "governance-snapshot-record-link"
    kind: Literal["snapshot_record_link"] = "snapshot_record_link"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    record_link_id: NonEmptyStr
    governance_snapshot_id: NonEmptyStr
    record_id: NonEmptyStr
    record_kind: NonEmptyStr
    role: SnapshotRecordRole = SnapshotRecordRole.CANONICAL
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_record_link(self) -> Self:
        _require_prefix(self.record_link_id, "govrecordlink", "record_link_id")
        _require_prefix(
            self.governance_snapshot_id, "govsnapshot", "governance_snapshot_id"
        )
        _require_prefix(self.record_id, "govrec", "record_id")
        return self


GovernanceSnapshotLink = Annotated[
    SnapshotAnchorLink | SnapshotDeltaLink | SnapshotRecordLink,
    Field(discriminator="kind"),
]
GOVERNANCE_SNAPSHOT_LINK_ADAPTER = TypeAdapter(GovernanceSnapshotLink)


class GovernanceQuestionCoverageLink(GovernanceModel):
    """Snapshot projection of shared CoverageEntry identities, not a control-plane copy."""

    schema_name: ClassVar[str] = "governance-question-coverage-link"
    kind: Literal["question_coverage_link"] = "question_coverage_link"
    question_id: QuestionId
    coverage_entry_ids: SortedUniqueStrings
    completeness_status: CompletenessStatus

    @model_validator(mode="after")
    def validate_coverage_link(self) -> Self:
        if not self.coverage_entry_ids:
            raise ValueError("question coverage links require shared coverage entry IDs")
        return self


class GovernanceSnapshot(GovernanceModel):
    schema_name: ClassVar[str] = "governance-snapshot"
    kind: Literal["governance_snapshot"] = "governance_snapshot"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    governance_snapshot_id: NonEmptyStr
    company_id: NonEmptyStr
    state_at: UtcDateTime
    known_at: UtcDateTime
    state_time_precision: TimePrecision
    known_time_precision: TimePrecision
    original_timezone: NonEmptyStr = "Asia/Shanghai"
    perspective: GovernancePerspective
    acquisition_scope: Literal["governance_management"] = GOVERNANCE_ACQUISITION_SCOPE
    question_set_id: Literal[
        "governance_management_questions"
    ] = GOVERNANCE_QUESTION_SET_ID
    question_set_version: NonEmptyStr
    source_registry_version: NonEmptyStr
    query_pack_version: NonEmptyStr
    extractor_versions: SortedUniqueStrings
    reconstruction_version: NonEmptyStr
    evidence_manifest_id: NonEmptyStr
    evidence_manifest_hash: Sha256Hex
    anchor_links: tuple[SnapshotAnchorLink, ...] = ()
    delta_links: tuple[SnapshotDeltaLink, ...] = ()
    record_links: tuple[SnapshotRecordLink, ...] = ()
    canonical_record_ids: SortedUniqueStrings = ()
    active_gap_ids: SortedUniqueStrings = ()
    active_conflict_ids: SortedUniqueStrings = ()
    pending_candidate_ids: SortedUniqueStrings = ()
    question_level_coverage: tuple[GovernanceQuestionCoverageLink, ...]
    completeness_status: CompletenessStatus
    future_knowledge_used: bool
    supersedes_snapshot_id: NonEmptyStr | None = None
    canonical_snapshot_hash: Sha256Hex
    created_at: UtcDateTime

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _require_prefix(
            self.governance_snapshot_id, "govsnapshot", "governance_snapshot_id"
        )
        _check_query_time(
            self.state_at,
            self.known_at,
            self.perspective,
            self.future_knowledge_used,
        )
        if not self.extractor_versions:
            raise ValueError("snapshot must freeze extractor versions")
        if not self.question_level_coverage:
            raise ValueError("snapshot must preserve question-level coverage")
        coverage_questions = [item.question_id for item in self.question_level_coverage]
        if coverage_questions != sorted(coverage_questions) or len(coverage_questions) != len(
            set(coverage_questions)
        ):
            raise ValueError("question-level coverage must be unique and stably sorted")
        expected_snapshot_id = self.governance_snapshot_id
        for link in (*self.anchor_links, *self.delta_links, *self.record_links):
            if link.governance_snapshot_id != expected_snapshot_id:
                raise ValueError("snapshot links cannot cross snapshot identity")
        anchor_ids = [link.anchor_link_id for link in self.anchor_links]
        record_link_ids = [link.record_link_id for link in self.record_links]
        if anchor_ids != sorted(anchor_ids) or record_link_ids != sorted(record_link_ids):
            raise ValueError("snapshot set-like links must be stably sorted")
        delta_sequences = [link.sequence for link in self.delta_links]
        if delta_sequences and delta_sequences != list(range(1, len(delta_sequences) + 1)):
            raise ValueError("snapshot delta sequence must be contiguous and ordered")
        linked_canonical = tuple(
            sorted(
                link.record_id
                for link in self.record_links
                if link.role == SnapshotRecordRole.CANONICAL
            )
        )
        if linked_canonical != self.canonical_record_ids:
            raise ValueError("canonical_record_ids must match canonical record links")
        if self.completeness_status == CompletenessStatus.COMPLETE and (
            self.active_gap_ids or self.active_conflict_ids
        ):
            raise ValueError("complete snapshots cannot have active gaps or conflicts")
        if self.completeness_status == CompletenessStatus.CONFLICTED and not self.active_conflict_ids:
            raise ValueError("conflicted snapshots require an active conflict")
        if self.supersedes_snapshot_id == self.governance_snapshot_id:
            raise ValueError("a snapshot cannot supersede itself")
        return self


class ObjectReference(GovernanceModel):
    schema_name: ClassVar[str] = "governance-object-reference"
    kind: Literal["object_reference"] = "object_reference"
    object_id: NonEmptyStr
    object_kind: NonEmptyStr
    schema_version: NonEmptyStr
    canonical_hash: Sha256Hex


class QuestionSummary(GovernanceModel):
    schema_name: ClassVar[str] = "governance-question-summary"
    kind: Literal["question_summary"] = "question_summary"
    question_id: QuestionId
    completeness_status: CompletenessStatus
    coverage_entry_ids: SortedUniqueStrings
    anchor_record_ids: SortedUniqueStrings = ()
    summary: NonEmptyStr

    @model_validator(mode="after")
    def validate_question_summary(self) -> Self:
        if not self.coverage_entry_ids:
            raise ValueError("question summaries require coverage identities")
        return self


class ToolSchemaReference(GovernanceModel):
    schema_name: ClassVar[str] = "governance-tool-schema-reference"
    kind: Literal["tool_schema_reference"] = "tool_schema_reference"
    tool_name: NonEmptyStr
    tool_version: NonEmptyStr
    input_schema_hash: Sha256Hex
    output_schema_hash: Sha256Hex
    read_only: Literal[True] = True


class ResearchBudget(GovernanceModel):
    schema_name: ClassVar[str] = "governance-research-budget"
    kind: Literal["research_budget"] = "research_budget"
    max_rounds: int = Field(ge=0)
    max_child_tasks: int = Field(ge=0)
    max_network_requests: int = Field(ge=0)
    max_parallelism: int = Field(ge=1)
    wall_clock_seconds: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1)
    max_recursion_depth: Literal[1] = 1


class BudgetUsage(GovernanceModel):
    schema_name: ClassVar[str] = "governance-budget-usage"
    kind: Literal["budget_usage"] = "budget_usage"
    rounds: int = Field(ge=0)
    child_tasks: int = Field(ge=0)
    network_requests: int = Field(ge=0)
    wall_clock_milliseconds: int = Field(ge=0)
    output_bytes: int = Field(ge=0)


class CodexInputPack(GovernanceModel):
    schema_name: ClassVar[str] = "governance-codex-input-pack"
    kind: Literal["codex_input_pack"] = "codex_input_pack"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    input_pack_id: NonEmptyStr
    company_id: NonEmptyStr
    state_at: UtcDateTime
    known_at: UtcDateTime
    perspective: GovernancePerspective
    governance_snapshot_id: NonEmptyStr
    governance_snapshot_hash: Sha256Hex
    evidence_manifest_id: NonEmptyStr
    evidence_manifest_hash: Sha256Hex
    question_set_id: Literal[
        "governance_management_questions"
    ] = GOVERNANCE_QUESTION_SET_ID
    question_set_version: NonEmptyStr
    question_summaries: tuple[QuestionSummary, ...]
    important_records: tuple[ObjectReference, ...] = ()
    important_event_ids: SortedUniqueStrings = ()
    active_gap_ids: SortedUniqueStrings = ()
    active_conflict_ids: SortedUniqueStrings = ()
    pending_candidate_ids: SortedUniqueStrings = ()
    tool_schemas: tuple[ToolSchemaReference, ...]
    research_budget: ResearchBudget
    temporal_rules: SortedUniqueStrings
    report_output_schema_hash: Sha256Hex
    canonical_hash: Sha256Hex
    created_at: UtcDateTime

    @model_validator(mode="after")
    def validate_input_pack(self) -> Self:
        _require_prefix(self.input_pack_id, "govinput", "input_pack_id")
        _require_prefix(
            self.governance_snapshot_id, "govsnapshot", "governance_snapshot_id"
        )
        _check_query_time(
            self.state_at,
            self.known_at,
            self.perspective,
            self.perspective == GovernancePerspective.RECONSTRUCTED
            and self.known_at > self.state_at,
        )
        question_ids = [item.question_id for item in self.question_summaries]
        if question_ids != sorted(question_ids) or len(question_ids) != len(set(question_ids)):
            raise ValueError("question summaries must be unique and stably sorted")
        record_ids = [item.object_id for item in self.important_records]
        if record_ids != sorted(record_ids) or len(record_ids) != len(set(record_ids)):
            raise ValueError("important records must be unique and stably sorted")
        tool_names = [item.tool_name for item in self.tool_schemas]
        if tool_names != sorted(tool_names) or len(tool_names) != len(set(tool_names)):
            raise ValueError("tool schemas must be unique and stably sorted")
        if not self.question_summaries or not self.tool_schemas or not self.temporal_rules:
            raise ValueError("input pack cannot omit question, tool, or temporal indexes")
        return self


class CodexToolRead(GovernanceModel):
    schema_name: ClassVar[str] = "governance-codex-tool-read"
    kind: Literal["codex_tool_read"] = "codex_tool_read"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    tool_read_id: NonEmptyStr
    session_id: NonEmptyStr
    governance_snapshot_id: NonEmptyStr
    sequence: int = Field(ge=1)
    tool_name: NonEmptyStr
    tool_version: NonEmptyStr
    canonical_parameters_json: NonEmptyStr
    parameters_hash: Sha256Hex
    response_payload_json: NonEmptyStr | None = None
    response_artifact_id: NonEmptyStr | None = None
    response_hash: Sha256Hex | None = None
    actual_record_ids: SortedUniqueStrings = ()
    actual_claim_ids: SortedUniqueStrings = ()
    actual_evidence_span_ids: SortedUniqueStrings = ()
    actual_raw_snapshot_ids: SortedUniqueStrings = ()
    citation_ids: SortedUniqueStrings = ()
    status: ToolReadStatus
    error_code: NonEmptyStr | None = None
    occurred_at: UtcDateTime
    canonical_hash: Sha256Hex

    @field_validator("canonical_parameters_json", "response_payload_json")
    @classmethod
    def validate_embedded_canonical_json(cls, value: str | None) -> str | None:
        if value is not None:
            load_canonical_json(value)
        return value

    @model_validator(mode="after")
    def validate_tool_read(self) -> Self:
        _require_prefix(self.tool_read_id, "govtoolread", "tool_read_id")
        _require_prefix(self.session_id, "govsession", "session_id")
        _require_prefix(
            self.governance_snapshot_id, "govsnapshot", "governance_snapshot_id"
        )
        if self.response_payload_json is not None and self.response_artifact_id is not None:
            raise ValueError("tool response must be inline or artifact-backed, not both")
        if self.status == ToolReadStatus.SUCCEEDED:
            if self.response_hash is None:
                raise ValueError("successful tool reads require a response hash")
            if self.response_payload_json is None and self.response_artifact_id is None:
                raise ValueError("successful tool reads require a persisted response")
            if self.error_code is not None:
                raise ValueError("successful tool reads cannot have an error code")
        elif self.error_code is None:
            raise ValueError("rejected or failed tool reads require an error code")
        for claim_id in self.actual_claim_ids:
            _require_prefix(claim_id, "govclaim", "actual_claim_ids")
        for span_id in self.actual_evidence_span_ids:
            _require_prefix(span_id, "govspan", "actual_evidence_span_ids")
        return self


class CodexSessionManifest(GovernanceModel):
    schema_name: ClassVar[str] = "governance-codex-session-manifest"
    kind: Literal["codex_session_manifest"] = "codex_session_manifest"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    session_manifest_id: NonEmptyStr
    parent_report_run_id: NonEmptyStr
    model_profile: NonEmptyStr
    runner_protocol_version: NonEmptyStr
    tool_protocol_version: NonEmptyStr
    input_pack_id: NonEmptyStr
    input_pack_hash: Sha256Hex
    initial_snapshot_id: NonEmptyStr
    final_snapshot_id: NonEmptyStr
    tool_read_ids: OrderedUniqueStrings = ()
    research_task_ids: OrderedUniqueStrings = ()
    research_result_bundle_ids: OrderedUniqueStrings = ()
    snapshot_adoption_ids: OrderedUniqueStrings = ()
    final_citation_ids: SortedUniqueStrings = ()
    report_hash: Sha256Hex | None = None
    generation_status: ReportGenerationStatus
    output_schema_validated: bool
    failure_code: NonEmptyStr | None = None
    started_at: UtcDateTime
    completed_at: UtcDateTime | None = None
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        _require_prefix(
            self.session_manifest_id, "govsession", "session_manifest_id"
        )
        _require_prefix(self.input_pack_id, "govinput", "input_pack_id")
        _require_prefix(self.initial_snapshot_id, "govsnapshot", "initial_snapshot_id")
        _require_prefix(self.final_snapshot_id, "govsnapshot", "final_snapshot_id")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("session completed_at must not precede started_at")
        if self.generation_status == ReportGenerationStatus.COMPLETED:
            if self.completed_at is None or self.report_hash is None or not self.output_schema_validated:
                raise ValueError("completed sessions require report hash and schema validation")
            if self.failure_code is not None:
                raise ValueError("completed sessions cannot have a failure code")
        if self.generation_status == ReportGenerationStatus.FAILED and self.failure_code is None:
            raise ValueError("failed sessions require a machine-readable failure code")
        return self


class ResearchTask(GovernanceModel):
    schema_name: ClassVar[str] = "governance-research-task"
    kind: Literal["research_task"] = "research_task"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    research_task_id: NonEmptyStr
    parent_session_id: NonEmptyStr
    company_id: NonEmptyStr
    question_ids: SortedUniqueQuestionIds
    gap_ids: SortedUniqueStrings
    question: NonEmptyStr
    state_at: UtcDateTime
    known_at: UtcDateTime
    perspective: GovernancePerspective
    known_evidence_ids: SortedUniqueStrings = ()
    allowed_source_roles: tuple[SourceRole, ...]
    budget: ResearchBudget
    recursion_depth: int = Field(ge=1, le=1)
    result_schema_name: NonEmptyStr
    result_schema_version: NonEmptyStr
    result_schema_hash: Sha256Hex
    created_at: UtcDateTime
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_research_task(self) -> Self:
        _require_prefix(
            self.research_task_id, "govresearchtask", "research_task_id"
        )
        _require_prefix(self.parent_session_id, "govsession", "parent_session_id")
        _check_query_time(
            self.state_at,
            self.known_at,
            self.perspective,
            self.perspective == GovernancePerspective.RECONSTRUCTED
            and self.known_at > self.state_at,
        )
        if not self.question_ids or not self.gap_ids:
            raise ValueError("research tasks require explicit question and gap IDs")
        if len(self.allowed_source_roles) != len(set(self.allowed_source_roles)):
            raise ValueError("allowed source roles must be unique")
        if tuple(role.value for role in self.allowed_source_roles) != tuple(
            sorted(role.value for role in self.allowed_source_roles)
        ):
            raise ValueError("allowed source roles must be stably sorted")
        if not self.allowed_source_roles:
            raise ValueError("research tasks require an explicit source allowlist")
        if SourceRole.DEFERRED in self.allowed_source_roles:
            raise ValueError("v1 research tasks cannot authorize deferred sources")
        return self


class AuthoritativeSourceCandidate(GovernanceModel):
    schema_name: ClassVar[str] = "governance-authoritative-source-candidate"
    kind: Literal["authoritative_source_candidate"] = "authoritative_source_candidate"
    item_id: NonEmptyStr
    source_role: SourceRole
    source_locator: NonEmptyStr
    title: NonEmptyStr | None = None
    announced_at: UtcDateTime | None = None
    available_at: UtcDateTime | None = None
    time_precision: TimePrecision | None = None
    payload_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_authoritative_candidate(self) -> Self:
        _require_prefix(self.item_id, "govresearchitem", "item_id")
        if self.source_role not in {
            SourceRole.OFFICIAL_DISCLOSURE,
            SourceRole.REGULATOR_EXCHANGE,
        }:
            raise ValueError("authoritative candidates require a formal source role")
        if (self.available_at is None) != (self.time_precision is None):
            raise ValueError("available_at and time_precision must be supplied together")
        return self


class ContextualEvidenceItem(GovernanceModel):
    schema_name: ClassVar[str] = "governance-contextual-evidence-item"
    kind: Literal["contextual_evidence"] = "contextual_evidence"
    item_id: NonEmptyStr
    source_role: Literal[SourceRole.CONTEXTUAL_EVIDENCE] = SourceRole.CONTEXTUAL_EVIDENCE
    source_locator: NonEmptyStr
    title: NonEmptyStr | None = None
    summary: NonEmptyStr
    available_at: UtcDateTime | None = None
    time_precision: TimePrecision | None = None
    payload_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_contextual(self) -> Self:
        _require_prefix(self.item_id, "govresearchitem", "item_id")
        if (self.available_at is None) != (self.time_precision is None):
            raise ValueError("available_at and time_precision must be supplied together")
        return self


class DiscoveryLead(GovernanceModel):
    schema_name: ClassVar[str] = "governance-discovery-lead"
    kind: Literal["discovery_lead"] = "discovery_lead"
    item_id: NonEmptyStr
    source_role: Literal[SourceRole.DISCOVERY_ONLY] = SourceRole.DISCOVERY_ONLY
    provider: NonEmptyStr
    locator: NonEmptyStr
    lead_text: NonEmptyStr | None = None
    payload_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_discovery(self) -> Self:
        _require_prefix(self.item_id, "govresearchitem", "item_id")
        return self


class DeferredResearchItem(GovernanceModel):
    schema_name: ClassVar[str] = "governance-deferred-research-item"
    kind: Literal["deferred_research_item"] = "deferred_research_item"
    item_id: NonEmptyStr
    source_role: Literal[SourceRole.DEFERRED] = SourceRole.DEFERRED
    source_family: NonEmptyStr
    reason_code: NonEmptyStr
    payload_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_deferred(self) -> Self:
        _require_prefix(self.item_id, "govresearchitem", "item_id")
        return self


class UnresolvedResearchGap(GovernanceModel):
    schema_name: ClassVar[str] = "governance-unresolved-research-gap"
    kind: Literal["unresolved_research_gap"] = "unresolved_research_gap"
    item_id: NonEmptyStr
    gap_id: NonEmptyStr
    reason_code: NonEmptyStr
    detail: NonEmptyStr

    @model_validator(mode="after")
    def validate_unresolved(self) -> Self:
        _require_prefix(self.item_id, "govresearchitem", "item_id")
        _require_prefix(self.gap_id, "govgap", "gap_id")
        return self


ResearchResultItem = Annotated[
    AuthoritativeSourceCandidate
    | ContextualEvidenceItem
    | DiscoveryLead
    | DeferredResearchItem
    | UnresolvedResearchGap,
    Field(discriminator="kind"),
]
RESEARCH_RESULT_ITEM_ADAPTER = TypeAdapter(ResearchResultItem)


def _item_ids(items: tuple[GovernanceModel, ...]) -> list[str]:
    return [str(getattr(item, "item_id")) for item in items]


class ResearchResultBundle(GovernanceModel):
    schema_name: ClassVar[str] = "governance-research-result-bundle"
    kind: Literal["research_result_bundle"] = "research_result_bundle"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    research_result_bundle_id: NonEmptyStr
    research_task_id: NonEmptyStr
    task_status: ResearchTaskStatus
    budget_used: BudgetUsage
    authoritative_source_candidates: tuple[AuthoritativeSourceCandidate, ...] = ()
    contextual_evidence: tuple[ContextualEvidenceItem, ...] = ()
    discovery_leads: tuple[DiscoveryLead, ...] = ()
    deferred_items: tuple[DeferredResearchItem, ...] = ()
    unresolved_gaps: tuple[UnresolvedResearchGap, ...] = ()
    failure_reason: NonEmptyStr | None = None
    created_at: UtcDateTime
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_result_bundle(self) -> Self:
        _require_prefix(
            self.research_result_bundle_id,
            "govresearchbundle",
            "research_result_bundle_id",
        )
        _require_prefix(self.research_task_id, "govresearchtask", "research_task_id")
        groups: tuple[tuple[GovernanceModel, ...], ...] = (
            self.authoritative_source_candidates,
            self.contextual_evidence,
            self.discovery_leads,
            self.deferred_items,
            self.unresolved_gaps,
        )
        seen: set[str] = set()
        for group in groups:
            ids = _item_ids(group)
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                raise ValueError("research result categories must be unique and stably sorted")
            overlap = seen.intersection(ids)
            if overlap:
                raise ValueError("research result items cannot appear in multiple categories")
            seen.update(ids)
        if self.task_status in {
            ResearchTaskStatus.FAILED,
            ResearchTaskStatus.BUDGET_EXHAUSTED,
        } and self.failure_reason is None:
            raise ValueError("failed or exhausted research requires a reason")
        if self.task_status == ResearchTaskStatus.COMPLETED and self.failure_reason is not None:
            raise ValueError("completed research cannot have a failure reason")
        return self


class QuarantinedResearchItem(GovernanceModel):
    """Parent-visible quarantine metadata; it intentionally has no semantic content."""

    schema_name: ClassVar[str] = "governance-quarantined-research-item"
    kind: Literal["quarantined_research_item"] = "quarantined_research_item"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    quarantine_id: NonEmptyStr
    research_task_id: NonEmptyStr
    research_result_bundle_id: NonEmptyStr
    source_item_id: NonEmptyStr
    reason_code: QuarantineReason
    quarantined_payload_artifact_id: NonEmptyStr
    quarantined_payload_hash: Sha256Hex
    known_at: UtcDateTime
    quarantined_at: UtcDateTime
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_quarantine(self) -> Self:
        _require_prefix(self.quarantine_id, "govquarantine", "quarantine_id")
        _require_prefix(self.research_task_id, "govresearchtask", "research_task_id")
        _require_prefix(
            self.research_result_bundle_id,
            "govresearchbundle",
            "research_result_bundle_id",
        )
        _require_prefix(self.source_item_id, "govresearchitem", "source_item_id")
        return self


class SnapshotAdoption(GovernanceModel):
    schema_name: ClassVar[str] = "governance-snapshot-adoption"
    kind: Literal["snapshot_adoption"] = "snapshot_adoption"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    snapshot_adoption_id: NonEmptyStr
    session_id: NonEmptyStr
    expected_session_revision: int = Field(ge=0)
    resulting_session_revision: int = Field(ge=1)
    old_snapshot_id: NonEmptyStr
    old_snapshot_hash: Sha256Hex
    new_snapshot_id: NonEmptyStr
    new_snapshot_hash: Sha256Hex
    adopted_at_sequence: int = Field(ge=1)
    adopted_at: UtcDateTime
    temporal_gate_passed: Literal[True] = True
    lineage_gate_passed: Literal[True] = True
    canonical_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_adoption(self) -> Self:
        _require_prefix(
            self.snapshot_adoption_id, "govadoption", "snapshot_adoption_id"
        )
        _require_prefix(self.session_id, "govsession", "session_id")
        _require_prefix(self.old_snapshot_id, "govsnapshot", "old_snapshot_id")
        _require_prefix(self.new_snapshot_id, "govsnapshot", "new_snapshot_id")
        if self.old_snapshot_id == self.new_snapshot_id:
            raise ValueError("snapshot adoption must switch to a new immutable snapshot")
        if self.resulting_session_revision != self.expected_session_revision + 1:
            raise ValueError("snapshot adoption revision must advance exactly once")
        return self


class FactualFinding(GovernanceModel):
    schema_name: ClassVar[str] = "governance-factual-finding"
    kind: Literal["fact"] = "fact"
    finding_id: NonEmptyStr
    text: NonEmptyStr
    citation_ids: SortedUniqueStrings
    uncertainty: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_factual_finding(self) -> Self:
        _require_prefix(self.finding_id, "govfinding", "finding_id")
        if not self.citation_ids:
            raise ValueError("factual findings require citations")
        return self


class ContextualFinding(GovernanceModel):
    schema_name: ClassVar[str] = "governance-contextual-finding"
    kind: Literal["contextual"] = "contextual"
    finding_id: NonEmptyStr
    text: NonEmptyStr
    citation_ids: SortedUniqueStrings
    source_role: Literal[SourceRole.CONTEXTUAL_EVIDENCE] = SourceRole.CONTEXTUAL_EVIDENCE
    uncertainty: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_contextual_finding(self) -> Self:
        _require_prefix(self.finding_id, "govfinding", "finding_id")
        if not self.citation_ids:
            raise ValueError("contextual findings require citations")
        return self


class CodexJudgment(GovernanceModel):
    schema_name: ClassVar[str] = "governance-codex-judgment"
    kind: Literal["judgment"] = "judgment"
    finding_id: NonEmptyStr
    text: NonEmptyStr
    citation_ids: SortedUniqueStrings
    uncertainty: NonEmptyStr

    @model_validator(mode="after")
    def validate_judgment(self) -> Self:
        _require_prefix(self.finding_id, "govfinding", "finding_id")
        if not self.citation_ids:
            raise ValueError("Codex judgments require evidence citations")
        return self


GovernanceFinding = Annotated[
    FactualFinding | ContextualFinding | CodexJudgment,
    Field(discriminator="kind"),
]
GOVERNANCE_FINDING_ADAPTER = TypeAdapter(GovernanceFinding)


class GovernanceReportSection(GovernanceModel):
    schema_name: ClassVar[str] = "governance-report-section"
    kind: Literal["governance_report_section"] = "governance_report_section"
    section_id: NonEmptyStr
    title: NonEmptyStr
    findings: tuple[GovernanceFinding, ...]

    @model_validator(mode="after")
    def validate_report_section(self) -> Self:
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("report finding IDs must be unique")
        return self


class ReportTechnicalValidation(GovernanceModel):
    schema_name: ClassVar[str] = "governance-report-technical-validation"
    kind: Literal["report_technical_validation"] = "report_technical_validation"
    passed: bool
    hard_failure_codes: SortedUniqueStrings = ()
    checks: tuple[ValidationCheck, ...]

    @model_validator(mode="after")
    def validate_technical_summary(self) -> Self:
        if not self.checks:
            raise ValueError("technical validation requires explicit checks")
        if self.passed and (self.hard_failure_codes or not all(item.passed for item in self.checks)):
            raise ValueError("passed technical validation cannot contain hard failures")
        if not self.passed and not self.hard_failure_codes:
            raise ValueError("failed technical validation requires hard failure codes")
        return self


class GovernanceReport(GovernanceModel):
    schema_name: ClassVar[str] = "governance-report"
    kind: Literal["governance_report"] = "governance_report"
    schema_version: NonEmptyStr = MODEL_SCHEMA_VERSION
    governance_report_id: NonEmptyStr
    company_id: NonEmptyStr
    state_at: UtcDateTime
    known_at: UtcDateTime
    perspective: GovernancePerspective
    governance_snapshot_id: NonEmptyStr
    governance_snapshot_hash: Sha256Hex
    evidence_manifest_id: NonEmptyStr
    evidence_manifest_hash: Sha256Hex
    session_manifest_id: NonEmptyStr
    session_manifest_hash: Sha256Hex
    generation_status: ReportGenerationStatus
    decision_author: Literal["codex"] = "codex"
    sections: tuple[GovernanceReportSection, ...]
    active_gap_ids: SortedUniqueStrings = ()
    active_conflict_ids: SortedUniqueStrings = ()
    pending_candidate_ids: SortedUniqueStrings = ()
    data_limitations: SortedUniqueStrings = ()
    citation_ids: SortedUniqueStrings
    future_knowledge_used: bool
    technical_validation: ReportTechnicalValidation
    failure_code: NonEmptyStr | None = None
    created_at: UtcDateTime
    canonical_report_hash: Sha256Hex

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        _require_prefix(
            self.governance_report_id, "govreport", "governance_report_id"
        )
        _require_prefix(
            self.governance_snapshot_id, "govsnapshot", "governance_snapshot_id"
        )
        _require_prefix(
            self.session_manifest_id, "govsession", "session_manifest_id"
        )
        _check_query_time(
            self.state_at,
            self.known_at,
            self.perspective,
            self.future_knowledge_used,
        )
        section_ids = [section.section_id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("report section IDs must be unique")
        finding_citations = {
            citation_id
            for section in self.sections
            for finding in section.findings
            for citation_id in finding.citation_ids
        }
        if not finding_citations.issubset(set(self.citation_ids)):
            raise ValueError("report citation index must include every finding citation")
        if self.generation_status == ReportGenerationStatus.COMPLETED:
            if not self.technical_validation.passed:
                raise ValueError("completed reports require passed technical validation")
            if self.failure_code is not None:
                raise ValueError("completed reports cannot carry a failure code")
        if self.generation_status == ReportGenerationStatus.FAILED:
            if self.technical_validation.passed or self.failure_code is None:
                raise ValueError("failed reports require technical failure diagnostics")
        return self


def parse_governance_record_json(
    data: bytes | bytearray | memoryview | str,
) -> GovernanceRecord:
    return validate_canonical_json(GOVERNANCE_RECORD_ADAPTER, data)


def parse_governance_snapshot_link_json(
    data: bytes | bytearray | memoryview | str,
) -> GovernanceSnapshotLink:
    return validate_canonical_json(GOVERNANCE_SNAPSHOT_LINK_ADAPTER, data)


def parse_research_result_item_json(
    data: bytes | bytearray | memoryview | str,
) -> ResearchResultItem:
    return validate_canonical_json(RESEARCH_RESULT_ITEM_ADAPTER, data)


def parse_governance_finding_json(
    data: bytes | bytearray | memoryview | str,
) -> GovernanceFinding:
    return validate_canonical_json(GOVERNANCE_FINDING_ADAPTER, data)


__all__ = [
    "AuditOpinionRecord",
    "AuditorEngagement",
    "AuthoritativeSourceCandidate",
    "BiographyClaim",
    "BudgetUsage",
    "CanonicalDecimal",
    "ClaimObjectType",
    "CodexInputPack",
    "CodexJudgment",
    "CodexSessionManifest",
    "CodexToolRead",
    "CommitmentRecord",
    "CompletenessStatus",
    "CompensationRecord",
    "ConflictRecord",
    "ContextualEvidenceItem",
    "ContextualFinding",
    "ControlRelation",
    "CorrectionRecord",
    "DecisionValue",
    "DeferredResearchItem",
    "DeltaDisposition",
    "DirectionKind",
    "DiscoveryLead",
    "ExtractionStatus",
    "ExtractorKind",
    "FactualFinding",
    "GOVERNANCE_FINDING_ADAPTER",
    "GOVERNANCE_RECORD_ADAPTER",
    "GOVERNANCE_SNAPSHOT_LINK_ADAPTER",
    "GapRecord",
    "GovernanceClaim",
    "GovernanceEvidenceSpan",
    "GovernanceExtractionRun",
    "GovernanceFinding",
    "GovernanceModel",
    "GovernancePerson",
    "GovernancePerspective",
    "GovernancePolicyVersion",
    "GovernanceQuestionCoverageLink",
    "GovernanceRecord",
    "GovernanceReport",
    "GovernanceReportSection",
    "GovernanceSnapshot",
    "GovernanceSnapshotLink",
    "Grant",
    "IncentiveGrant",
    "IncentivePlan",
    "InquiryRecord",
    "InternalControlRecord",
    "LitigationMatter",
    "MODEL_SCHEMA_VERSION",
    "ObjectReference",
    "OwnershipPosition",
    "OwnershipSnapshot",
    "PersonAlias",
    "PersonLinkCandidate",
    "PersonLinkDecision",
    "PledgePositionSnapshot",
    "QuestionSummary",
    "QuarantineReason",
    "QuarantinedResearchItem",
    "RESEARCH_RESULT_ITEM_ADAPTER",
    "RecordResolutionStatus",
    "RegulatoryMatter",
    "RelatedPartyRelation",
    "RelatedPartyTransaction",
    "ReportGenerationStatus",
    "ReportTechnicalValidation",
    "ResearchBudget",
    "ResearchResultBundle",
    "ResearchResultItem",
    "ResearchTask",
    "ResearchTaskStatus",
    "ReviewDecision",
    "ReviewStatus",
    "RoleTenure",
    "RosterSnapshot",
    "Sha256Hex",
    "SnapshotAdoption",
    "SnapshotAnchorLink",
    "SnapshotDeltaLink",
    "SnapshotRecordLink",
    "SnapshotRecordRole",
    "SourceRole",
    "TimePrecision",
    "ToolReadStatus",
    "ToolSchemaReference",
    "UnresolvedResearchGap",
    "UtcDateTime",
    "ValidationCheck",
    "ValidationResult",
    "VerificationStatus",
    "VestingCondition",
    "parse_governance_finding_json",
    "parse_governance_record_json",
    "parse_governance_snapshot_link_json",
    "parse_research_result_item_json",
]
