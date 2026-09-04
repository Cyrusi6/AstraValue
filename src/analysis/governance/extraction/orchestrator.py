from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from ..admission import admission_failures, canonical_eligible
from ..canonical import canonical_sha256
from ..models import (
    AuditOpinionRecord,
    AuditorEngagement,
    BiographyClaim,
    ClaimObjectType,
    CommitmentRecord,
    CompletenessStatus,
    CompensationRecord,
    ControlRelation,
    CorrectionRecord,
    ExtractionStatus,
    ExtractorKind,
    GovernanceClaim,
    GovernanceEvidenceSpan,
    GovernanceExtractionRun,
    GovernancePerson,
    GovernancePolicyVersion,
    IncentiveGrant,
    IncentivePlan,
    InquiryRecord,
    InternalControlRecord,
    LitigationMatter,
    OwnershipPosition,
    OwnershipSnapshot,
    PledgePositionSnapshot,
    RegulatoryMatter,
    RelatedPartyRelation,
    RelatedPartyTransaction,
    ReviewStatus,
    RoleTenure,
    RosterSnapshot,
    SourceRole,
    TimePrecision,
    ValidationResult,
    VerificationStatus,
    VestingCondition,
)
from .candidates import CandidateExtractor
from .deterministic import (
    ClaimFieldDraft,
    DeterministicExtraction,
    DeterministicTableExtractor,
    RecordDraft,
)
from .loader import ManifestBoundArtifact, ManifestBoundExtractionLoader
from .validators import GovernanceFieldValidator


_RECORD_CLASSES: dict[str, type[Any]] = {
    "roster_snapshot": RosterSnapshot,
    "role_tenure": RoleTenure,
    "ownership_position": OwnershipPosition,
    "ownership_snapshot": OwnershipSnapshot,
    "control_relation": ControlRelation,
    "pledge_position_snapshot": PledgePositionSnapshot,
    "compensation_record": CompensationRecord,
    "related_party_relation": RelatedPartyRelation,
    "related_party_transaction": RelatedPartyTransaction,
    "incentive_plan": IncentivePlan,
    "incentive_grant": IncentiveGrant,
    "vesting_condition": VestingCondition,
    "auditor_engagement": AuditorEngagement,
    "audit_opinion_record": AuditOpinionRecord,
    "internal_control_record": InternalControlRecord,
    "regulatory_matter": RegulatoryMatter,
    "inquiry_record": InquiryRecord,
    "litigation_matter": LitigationMatter,
    "commitment_record": CommitmentRecord,
    "governance_policy_version": GovernancePolicyVersion,
    "correction_record": CorrectionRecord,
}
_AUTHORITATIVE = frozenset(
    {SourceRole.OFFICIAL_DISCLOSURE, SourceRole.REGULATOR_EXCHANGE}
)


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    record_type: str
    record_key: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    run: GovernanceExtractionRun
    evidence_spans: tuple[GovernanceEvidenceSpan, ...]
    claims: tuple[GovernanceClaim, ...]
    validation_results: tuple[ValidationResult, ...]
    canonical_records: tuple[object, ...]
    candidate_claim_ids: tuple[str, ...]
    rejected_records: tuple[RejectedRecord, ...]
    title_recalls: tuple[object, ...]


class ExtractionSink(Protocol):
    """Append-only governance persistence seam; no shared-kernel ownership."""

    def append_evidence_span(self, span: GovernanceEvidenceSpan, /) -> None: ...

    def append_claim(self, claim: GovernanceClaim, /) -> None: ...

    def append_validation_result(self, result: ValidationResult, /) -> None: ...

    def append_record(self, record: object, /) -> None: ...

    def append_run(self, run: GovernanceExtractionRun, /) -> None: ...


@dataclass(slots=True)
class InMemoryExtractionSink:
    """Test/reference sink that rejects overwrites and exposes write order."""

    evidence_spans: dict[str, GovernanceEvidenceSpan] = field(default_factory=dict)
    claims: dict[str, GovernanceClaim] = field(default_factory=dict)
    validation_results: dict[str, ValidationResult] = field(default_factory=dict)
    records: dict[str, object] = field(default_factory=dict)
    runs: dict[str, GovernanceExtractionRun] = field(default_factory=dict)
    write_order: list[tuple[str, str]] = field(default_factory=list)

    @staticmethod
    def _append(store: dict[str, object], key: str, value: object) -> None:
        if key in store:
            raise ValueError(f"append-only identity already exists: {key}")
        store[key] = value

    def append_evidence_span(self, span: GovernanceEvidenceSpan, /) -> None:
        self._append(self.evidence_spans, span.evidence_span_id, span)
        self.write_order.append(("span", span.evidence_span_id))

    def append_claim(self, claim: GovernanceClaim, /) -> None:
        self._append(self.claims, claim.claim_id, claim)
        self.write_order.append(("claim", claim.claim_id))

    def append_validation_result(self, result: ValidationResult, /) -> None:
        self._append(
            self.validation_results, result.validation_result_id, result
        )
        self.write_order.append(("validation", result.validation_result_id))

    def append_record(self, record: object, /) -> None:
        identity = next(
            (
                value
                for name in (
                    "record_id",
                    "biography_claim_id",
                    "position_id",
                    "person_id",
                )
                if isinstance((value := getattr(record, name, None)), str)
            ),
            None,
        )
        if not isinstance(identity, str):
            raise TypeError("record has no append-only governance identity")
        self._append(self.records, identity, record)
        self.write_order.append(("record", identity))

    def append_run(self, run: GovernanceExtractionRun, /) -> None:
        self._append(self.runs, run.extraction_run_id, run)
        self.write_order.append(("run", run.extraction_run_id))


def _hash(schema_name: str, payload: Mapping[str, object], version: str = "1.0.0") -> str:
    return canonical_sha256(
        payload, schema_name=schema_name, schema_version=version
    )


def _record_identity(draft: RecordDraft, artifact: ManifestBoundArtifact) -> str:
    digest = _hash(
        "governance-record-id",
        {
            "company_id": draft.company_id,
            "question_id": draft.question_id,
            "record_type": draft.record_type,
            "record_key": draft.record_key,
            "manifest_id": artifact.manifest_id,
            "raw_snapshot_id": artifact.raw_snapshot_id,
        },
    )
    if draft.record_type == "governance_person":
        value = draft.values.get("person_id")
        return str(value) if value is not None else f"govp:{draft.company_id}:{digest[:24]}"
    if draft.record_type == "biography_claim":
        return f"govbio:{digest[:32]}"
    if draft.record_type == "ownership_position":
        return f"govposition:{digest[:32]}"
    return f"govrec:{draft.record_type}:{digest[:32]}"


def _source_role(draft: RecordDraft, artifact: ManifestBoundArtifact) -> SourceRole:
    value = draft.lineage_overrides.get("source_role", artifact.source_role)
    return value if isinstance(value, SourceRole) else SourceRole(str(value))


def _independence_group(draft: RecordDraft, artifact: ManifestBoundArtifact) -> str:
    value = draft.lineage_overrides.get(
        "independence_group", artifact.independence_group
    )
    return str(value)


def _upstream_material_id(draft: RecordDraft, artifact: ManifestBoundArtifact) -> str:
    value = draft.lineage_overrides.get(
        "upstream_material_id", artifact.upstream_material_id
    )
    return str(value)


def _semantic_value(value: object) -> str:
    return _hash("governance-conflict-value", {"value": value})


def conflicting_draft_fields(
    drafts: Sequence[RecordDraft], artifact: ManifestBoundArtifact
) -> frozenset[tuple[int, int]]:
    """Locate incompatible facts; mirrors with the same value never add a vote."""

    grouped: dict[tuple[object, ...], list[tuple[int, int, str, str]]] = {}
    for draft_index, draft in enumerate(drafts):
        if _source_role(draft, artifact) not in _AUTHORITATIVE:
            continue
        for field_index, field_draft in enumerate(draft.fields):
            key = (
                draft.company_id,
                draft.question_id,
                draft.subject_id,
                field_draft.predicate,
                draft.record_type,
                draft.record_key if draft.record_type == "role_tenure" else None,
                draft.values.get("reference_at"),
                draft.values.get("effective_at"),
            )
            grouped.setdefault(key, []).append(
                (
                    draft_index,
                    field_index,
                    _independence_group(draft, artifact),
                    _semantic_value(field_draft.object_value),
                )
            )
    conflicted: set[tuple[int, int]] = set()
    for values in grouped.values():
        by_group: dict[str, set[str]] = {}
        for _draft_index, _field_index, group, value_hash in values:
            by_group.setdefault(group, set()).add(value_hash)
        independent_values = {item for hashes in by_group.values() for item in hashes}
        # Different values are unsafe even within one upstream material; two
        # access mirrors with the same value collapse to one independence group.
        if len(independent_values) > 1:
            conflicted.update((item[0], item[1]) for item in values)
    return frozenset(conflicted)


def independent_source_count(claims: Iterable[GovernanceClaim]) -> int:
    """Count real upstream groups, not access mirrors or duplicate claims."""

    return len({claim.independence_group for claim in claims})


def detect_claim_conflicts(
    claims: Iterable[GovernanceClaim],
) -> frozenset[str]:
    grouped: dict[tuple[object, ...], list[GovernanceClaim]] = {}
    for claim in claims:
        if claim.source_role not in _AUTHORITATIVE:
            continue
        key = (
            claim.company_id,
            claim.question_id,
            claim.subject_id,
            claim.predicate,
            claim.record_type,
            claim.target_record_id if claim.record_type == "role_tenure" else None,
            claim.reference_at,
            claim.effective_at,
        )
        grouped.setdefault(key, []).append(claim)
    conflicted: set[str] = set()
    for values in grouped.values():
        by_group: dict[str, set[str]] = {}
        for claim in values:
            by_group.setdefault(claim.independence_group, set()).add(
                _semantic_value(claim.object_value)
            )
        if len({item for hashes in by_group.values() for item in hashes}) > 1:
            conflicted.update(claim.claim_id for claim in values)
    return frozenset(conflicted)


def _span(
    artifact: ManifestBoundArtifact,
    draft: RecordDraft,
    field_draft: ClaimFieldDraft,
) -> GovernanceEvidenceSpan:
    locator = field_draft.locator
    if locator is None or not locator.structurally_complete:
        raise ValueError("evidence_locator_incomplete")
    # Table scope is sufficient only for a declared complete roster; all
    # ordinary fields must locate their exact row/column or paragraph/range.
    if (
        not locator.field_level_complete
        and not (
            draft.record_type == "roster_snapshot"
            and field_draft.field_name == "member_ids"
            and locator.table_id is not None
        )
    ):
        raise ValueError("evidence_locator_incomplete")
    locator_payload: dict[str, object] = {
        "raw_snapshot_id": artifact.raw_snapshot_id,
        "content_hash": artifact.content_hash,
        "derived_artifact_id": artifact.derived_artifact_id,
        "derived_artifact_hash": artifact.derived_artifact_hash,
        "page": locator.page,
        "table_id": locator.table_id,
        "row_label": locator.row_label,
        "column_label": locator.column_label,
        "paragraph_id": locator.paragraph_id,
        "char_start": locator.char_start,
        "char_end": locator.char_end,
        "field_name": field_draft.field_name,
    }
    digest = _hash("governance-evidence-span-id", locator_payload)
    excerpt = locator.excerpt
    if excerpt is None:
        excerpt = str(field_draft.object_value)
    payload: dict[str, object] = {
        **{key: value for key, value in locator_payload.items() if key != "field_name"},
        "evidence_span_id": f"govspan:{digest[:32]}",
        "excerpt_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }
    return GovernanceEvidenceSpan.model_validate(payload, strict=True)


def _claim_payload(
    *,
    run_id: str,
    artifact: ManifestBoundArtifact,
    draft: RecordDraft,
    field_draft: ClaimFieldDraft,
    span: GovernanceEvidenceSpan,
    extractor_kind: ExtractorKind,
    extractor_version: str,
    target_record_id: str,
    conflicted: bool,
) -> dict[str, object]:
    if artifact.announced_at is None:
        raise ValueError("announced_at_missing")
    if artifact.available_at is None:
        raise ValueError("available_at_missing")
    if artifact.retrieved_at is None:
        raise ValueError("retrieved_at_missing")
    identity = _hash(
        "governance-claim-id",
        {
            "run_id": run_id,
            "record_key": draft.record_key,
            "field_name": field_draft.field_name,
            "span_id": span.evidence_span_id,
            "extractor_version": extractor_version,
        },
    )
    review_status = (
        ReviewStatus.NOT_REQUIRED
        if extractor_kind == ExtractorKind.DETERMINISTIC
        else ReviewStatus.PENDING
    )
    payload: dict[str, object] = {
        "claim_id": f"govclaim:{identity[:32]}",
        "extraction_run_id": run_id,
        "company_id": draft.company_id,
        "question_id": draft.question_id,
        "subject_type": draft.subject_type,
        "subject_id": draft.subject_id,
        "predicate": field_draft.predicate,
        "object_type": field_draft.object_type,
        "object_value": field_draft.object_value,
        "unit": field_draft.unit,
        "currency": field_draft.currency,
        "record_type": draft.record_type,
        "target_record_id": target_record_id,
        "reference_at": draft.values.get("reference_at"),
        "effective_at": draft.values.get("effective_at"),
        "valid_from": draft.values.get("valid_from"),
        "valid_to": draft.values.get("valid_to"),
        "announced_at": artifact.announced_at,
        "available_at": artifact.available_at,
        "retrieved_at": artifact.retrieved_at,
        "original_timezone": "Asia/Shanghai",
        "time_precision": TimePrecision.DATETIME,
        "source_role": _source_role(draft, artifact),
        "evidence_manifest_id": artifact.manifest_id,
        "raw_snapshot_id": artifact.raw_snapshot_id,
        "content_hash": artifact.content_hash,
        "evidence_span_id": span.evidence_span_id,
        "upstream_material_id": _upstream_material_id(draft, artifact),
        "independence_group": _independence_group(draft, artifact),
        "extractor_kind": extractor_kind,
        "extractor_version": extractor_version,
        "extraction_status": draft.extraction_status,
        "verification_status": (
            VerificationStatus.CONFLICTED
            if conflicted
            else VerificationStatus.NOT_APPLICABLE
        ),
        "review_status": review_status,
        "raw_hash_verified": artifact.integrity_verified,
        "lineage_complete": artifact.lineage_complete,
        "evidence_locator_complete": span.locator_complete,
        "has_unresolved_conflict": conflicted,
    }
    return payload


def _reverify_claim(
    claim: GovernanceClaim,
    status: VerificationStatus,
    *,
    conflicted: bool,
) -> GovernanceClaim:
    payload = claim.model_dump(mode="python")
    payload.update(
        verification_status=status,
        has_unresolved_conflict=conflicted,
    )
    return GovernanceClaim.model_validate_with_hash(payload)


def _model_payload(
    model_cls: type[Any], values: Mapping[str, object], injected: Mapping[str, object]
) -> dict[str, object]:
    allowed = set(model_cls.model_fields)
    payload = {key: value for key, value in values.items() if key in allowed}
    payload.update({key: value for key, value in injected.items() if key in allowed})
    return payload


def _materialize_ownership_position(
    raw: Mapping[str, object],
    *,
    identity: str,
    draft: RecordDraft,
    claim_ids: tuple[str, ...],
    span_ids: tuple[str, ...],
) -> OwnershipPosition:
    payload = _model_payload(
        OwnershipPosition,
        raw,
        {
            "position_id": identity,
            "company_id": draft.company_id,
            "completeness_status": draft.completeness_status,
            "claim_ids": claim_ids,
            "evidence_span_ids": span_ids,
        },
    )
    return OwnershipPosition.model_validate_with_hash(payload)


def _materialize_record(
    *,
    artifact: ManifestBoundArtifact,
    draft: RecordDraft,
    target_record_id: str,
    claims: Sequence[GovernanceClaim],
    spans: Sequence[GovernanceEvidenceSpan],
) -> object:
    claim_ids = tuple(sorted(claim.claim_id for claim in claims))
    span_ids = tuple(sorted(span.evidence_span_id for span in spans))
    if artifact.available_at is None:
        raise ValueError("available_at_missing")
    available_precisions = {claim.time_precision for claim in claims}
    if len(available_precisions) != 1:
        raise ValueError("available_time_precision_conflict")
    available_time_precision = next(iter(available_precisions))

    if draft.record_type == "governance_person":
        payload = _model_payload(
            GovernancePerson,
            draft.values,
            {
                "person_id": target_record_id,
                "company_id": draft.company_id,
                "source_claim_ids": claim_ids,
                "available_at": artifact.available_at,
            },
        )
        return GovernancePerson.model_validate_with_hash(payload)

    if draft.record_type == "biography_claim":
        payload = _model_payload(
            BiographyClaim,
            draft.values,
            {
                "biography_claim_id": target_record_id,
                "company_id": draft.company_id,
                "claim_id": claim_ids[0],
                "source_role": _source_role(draft, artifact),
                "raw_snapshot_id": artifact.raw_snapshot_id,
                "evidence_span_id": span_ids[0],
                "available_at": artifact.available_at,
                "extractor_version": claims[0].extractor_version,
            },
        )
        return BiographyClaim.model_validate_with_hash(payload)

    if draft.record_type == "ownership_position":
        return _materialize_ownership_position(
            draft.values,
            identity=target_record_id,
            draft=draft,
            claim_ids=claim_ids,
            span_ids=span_ids,
        )

    model_cls = _RECORD_CLASSES[draft.record_type]
    values = dict(draft.values)
    if draft.record_type == "ownership_snapshot" and isinstance(
        values.get("positions"), Sequence
    ):
        positions: list[OwnershipPosition] = []
        for index, raw in enumerate(values["positions"]):
            if not isinstance(raw, Mapping):
                raise ValueError("ownership position must be an object")
            digest = _hash(
                "governance-position-id",
                {
                    "parent_record_id": target_record_id,
                    "index": index,
                    "holder_entity_id": raw.get("holder_entity_id"),
                },
            )
            positions.append(
                _materialize_ownership_position(
                    raw,
                    identity=f"govposition:{digest[:32]}",
                    draft=draft,
                    claim_ids=claim_ids,
                    span_ids=span_ids,
                )
            )
        values["positions"] = tuple(sorted(positions, key=lambda item: item.position_id))

    injected: dict[str, object] = {
        "record_id": target_record_id,
        "company_id": draft.company_id,
        "question_id": draft.question_id,
        "claim_ids": claim_ids,
        "evidence_span_ids": span_ids,
        "source_event_ids": tuple(sorted(values.get("source_event_ids", ()))),
        "reference_at": values.get("reference_at"),
        "effective_at": values.get("effective_at"),
        "valid_from": values.get("valid_from"),
        "valid_to": values.get("valid_to"),
        "available_at": artifact.available_at,
        "available_time_precision": available_time_precision,
        "completeness_status": CompletenessStatus.COMPLETE,
        "supersedes_record_id": values.get("supersedes_record_id"),
    }
    if draft.record_type in {"roster_snapshot", "ownership_snapshot"}:
        injected["source_manifest_id"] = artifact.manifest_id
    if draft.record_type == "roster_snapshot":
        injected["completeness_evidence_span_ids"] = span_ids
    if draft.record_type == "role_tenure":
        role_span = next(
            (
                span.evidence_span_id
                for claim, span in zip(claims, spans, strict=True)
                if claim.predicate in {"role_code", "valid_from"}
            ),
            span_ids[0],
        )
        injected["appointment_evidence_span_id"] = role_span
        if values.get("valid_to") is not None:
            termination = next(
                (
                    span.evidence_span_id
                    for claim, span in zip(claims, spans, strict=True)
                    if claim.predicate == "valid_to"
                ),
                None,
            )
            if termination is None:
                raise ValueError("termination_evidence_missing")
            injected["termination_evidence_span_id"] = termination

    payload = _model_payload(model_cls, values, injected)
    return model_cls.model_validate_with_hash(payload)


class GovernanceExtractionOrchestrator:
    """Manifest-bound claim-first extraction and deterministic admission."""

    def __init__(
        self,
        loader: ManifestBoundExtractionLoader,
        *,
        validator: GovernanceFieldValidator | None = None,
        sink: ExtractionSink | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        target_schema_version: str = "1.0.0",
    ) -> None:
        self._loader = loader
        self._validator = validator or GovernanceFieldValidator()
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._target_schema_version = target_schema_version

    def execute(
        self,
        *,
        manifest_id: str,
        artifact_id: str,
        company_id: str,
        extractor: DeterministicTableExtractor | CandidateExtractor | None = None,
    ) -> OrchestrationResult:
        selected = extractor or DeterministicTableExtractor()
        if isinstance(selected, CandidateExtractor):
            extractor_kind = selected.extractor_kind
            for_llm = selected.for_llm
        else:
            extractor_kind = ExtractorKind.DETERMINISTIC
            for_llm = False
        artifact = self._loader.load(
            manifest_id, artifact_id, for_llm=for_llm
        )
        started_at = self._clock()
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("orchestrator clock must return timezone-aware datetimes")
        run_id = f"govxrun:{self._id_factory()}"
        extracted: DeterministicExtraction = selected.extract(
            artifact, company_id=company_id
        )
        drafts = extracted.record_drafts
        conflicts = conflicting_draft_fields(drafts, artifact)

        all_spans: list[GovernanceEvidenceSpan] = []
        all_claims: list[GovernanceClaim] = []
        all_results: list[ValidationResult] = []
        canonical_records: list[object] = []
        candidate_ids: list[str] = []
        rejected: list[RejectedRecord] = []
        per_draft: list[
            list[tuple[GovernanceClaim, GovernanceEvidenceSpan, ValidationResult]]
        ] = [[] for _ in drafts]

        for draft_index, draft in enumerate(drafts):
            target_id = _record_identity(draft, artifact)
            for field_index, field_draft in enumerate(draft.fields):
                is_conflicted = (draft_index, field_index) in conflicts
                try:
                    span = _span(artifact, draft, field_draft)
                    provisional = GovernanceClaim.model_validate_with_hash(
                        _claim_payload(
                            run_id=run_id,
                            artifact=artifact,
                            draft=draft,
                            field_draft=field_draft,
                            span=span,
                            extractor_kind=extractor_kind,
                            extractor_version=selected.version,
                            target_record_id=target_id,
                            conflicted=is_conflicted,
                        )
                    )
                except (TypeError, ValueError, ValidationError) as exc:
                    rejected.append(
                        RejectedRecord(
                            draft.record_type,
                            draft.record_key,
                            (f"broken_span_or_lineage:{exc}",),
                        )
                    )
                    continue
                validation = self._validator.validate(
                    provisional,
                    evidence_span=span,
                    draft=draft,
                    validated_at=self._clock(),
                    primary_key_unique=(
                        "primary_key_duplicate" not in draft.issue_codes
                    ),
                    source_conflict=is_conflicted,
                )
                claim = _reverify_claim(
                    provisional,
                    validation.verification_status,
                    conflicted=is_conflicted,
                )
                if isinstance(selected, CandidateExtractor):
                    claim = selected.isolate_claim(claim)
                all_spans.append(span)
                all_claims.append(claim)
                all_results.append(validation)
                per_draft[draft_index].append((claim, span, validation))
                if claim.review_status == ReviewStatus.PENDING:
                    candidate_ids.append(claim.claim_id)
                if self._sink is not None:
                    self._sink.append_evidence_span(span)
                    self._sink.append_claim(claim)
                    self._sink.append_validation_result(validation)

        seen_records: set[str] = set()
        for draft, triples in zip(drafts, per_draft, strict=True):
            if not triples:
                if not any(
                    item.record_type == draft.record_type
                    and item.record_key == draft.record_key
                    for item in rejected
                ):
                    rejected.append(
                        RejectedRecord(
                            draft.record_type,
                            draft.record_key,
                            ("no_materialized_claims",),
                        )
                    )
                continue
            claims = tuple(item[0] for item in triples)
            spans = tuple(item[1] for item in triples)
            validations = tuple(item[2] for item in triples)
            failures: list[str] = list(draft.issue_codes)
            for claim, span, validation in triples:
                failures.extend(
                    admission_failures(
                        claim,
                        evidence_span=span,
                        validation_results=(validation,),
                    )
                )
            if not all(
                canonical_eligible(
                    claim,
                    evidence_span=span,
                    validation_results=(validation,),
                )
                for claim, span, validation in triples
            ):
                rejected.append(
                    RejectedRecord(
                        draft.record_type,
                        draft.record_key,
                        tuple(dict.fromkeys(failures or ("canonical_admission_failed",))),
                    )
                )
                continue
            target_id = claims[0].target_record_id
            if target_id is None:
                rejected.append(
                    RejectedRecord(
                        draft.record_type,
                        draft.record_key,
                        ("target_record_id_missing",),
                    )
                )
                continue
            try:
                record = _materialize_record(
                    artifact=artifact,
                    draft=draft,
                    target_record_id=target_id,
                    claims=claims,
                    spans=spans,
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                rejected.append(
                    RejectedRecord(
                        draft.record_type,
                        draft.record_key,
                        (f"typed_record_invalid:{exc}",),
                    )
                )
                continue
            identity = str(
                next(
                    (
                        value
                        for name in (
                            "record_id",
                            "biography_claim_id",
                            "position_id",
                            "person_id",
                        )
                        if isinstance(
                            (value := getattr(record, name, None)), str
                        )
                    ),
                    "",
                )
            )
            if identity in seen_records:
                continue
            seen_records.add(identity)
            canonical_records.append(record)
            if self._sink is not None:
                self._sink.append_record(record)

        completed_at = self._clock()
        if completed_at < started_at:
            raise ValueError("orchestrator clock moved backwards")
        run_payload: dict[str, object] = {
            "extraction_run_id": run_id,
            "evidence_manifest_id": artifact.manifest_id,
            "raw_snapshot_id": artifact.raw_snapshot_id,
            "content_hash": artifact.content_hash,
            "question_ids": tuple(sorted({draft.question_id for draft in drafts})),
            "extractor_kind": extractor_kind,
            "extractor_name": selected.name,
            "extractor_version": selected.version,
            "target_schema_version": self._target_schema_version,
            "started_at": started_at,
            "completed_at": completed_at,
            "output_claim_ids": tuple(sorted(claim.claim_id for claim in all_claims)),
            "validation_result_ids": tuple(
                sorted(result.validation_result_id for result in all_results)
            ),
        }
        run = GovernanceExtractionRun.model_validate_with_hash(
            run_payload,
            hash_field="run_hash",
        )
        if self._sink is not None:
            self._sink.append_run(run)
        return OrchestrationResult(
            run=run,
            evidence_spans=tuple(all_spans),
            claims=tuple(all_claims),
            validation_results=tuple(all_results),
            canonical_records=tuple(canonical_records),
            candidate_claim_ids=tuple(sorted(candidate_ids)),
            rejected_records=tuple(rejected),
            title_recalls=extracted.title_recalls,
        )

    # Familiar alias for callers that name pipeline execution `run`.
    run = execute


ExtractionOrchestrator = GovernanceExtractionOrchestrator


__all__ = [
    "ExtractionOrchestrator",
    "ExtractionSink",
    "GovernanceExtractionOrchestrator",
    "InMemoryExtractionSink",
    "OrchestrationResult",
    "RejectedRecord",
    "conflicting_draft_fields",
    "detect_claim_conflicts",
    "independent_source_count",
]
