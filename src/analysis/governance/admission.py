from __future__ import annotations

from pydantic import ValidationError

from .models import (
    DecisionValue,
    ExtractionStatus,
    ExtractorKind,
    GovernanceClaim,
    GovernanceEvidenceSpan,
    ReviewDecision,
    ReviewStatus,
    SourceRole,
    ValidationResult,
    VerificationStatus,
)


AUTHORITATIVE_SOURCE_ROLES = frozenset(
    {
        SourceRole.OFFICIAL_DISCLOSURE,
        SourceRole.REGULATOR_EXCHANGE,
    }
)

GOVERNANCE_FIELD_VALIDATOR_ID = ("governance-field-validator", "1.0.0")
REQUIRED_AUTOMATIC_VALIDATION_CHECK_CODES = frozenset(
    {
        "field_type_valid",
        "required_fields_present",
        "decimal_valid",
        "unit_valid",
        "currency_iso4217",
        "date_order_valid",
        "period_order_valid",
        "primary_key_present",
        "primary_key_unique",
        "total_reconciles",
        "ratio_basis_present",
        "evidence_span_bound",
        "company_namespace_valid",
        "person_namespace_valid",
        "amount_currency_pair_valid",
        "domain_nonnegative",
        "extraction_draft_complete",
        "source_conflict_absent",
    }
)


def _external_validation_failures(
    claim: GovernanceClaim,
    *,
    evidence_span: GovernanceEvidenceSpan | None,
    validation_results: tuple[ValidationResult, ...],
) -> tuple[str, ...]:
    """Validate evidence supplied independently from a claim's self-reported flags."""

    failures: list[str] = []
    if evidence_span is None:
        failures.append("evidence_span_missing")
    elif not isinstance(evidence_span, GovernanceEvidenceSpan):
        failures.append("evidence_span_invalid")
    else:
        try:
            GovernanceEvidenceSpan.model_validate(
                evidence_span.model_dump(mode="python"), strict=True
            )
        except ValidationError:
            failures.append("evidence_span_invalid")
        if evidence_span.evidence_span_id != claim.evidence_span_id:
            failures.append("evidence_span_mismatch")
        if evidence_span.raw_snapshot_id != claim.raw_snapshot_id:
            failures.append("raw_snapshot_mismatch")
        if evidence_span.content_hash != claim.content_hash:
            failures.append("content_hash_mismatch")
        if not evidence_span.locator_complete:
            failures.append("evidence_locator_incomplete")

    if not validation_results:
        failures.append("validation_results_missing")
        return tuple(dict.fromkeys(failures))

    result_ids: set[str] = set()
    passed_check_codes: set[str] = set()
    evidence_binding_proven = False
    approved_validator_seen = False
    for result in validation_results:
        if not isinstance(result, ValidationResult):
            failures.append("validation_result_invalid")
            continue
        try:
            ValidationResult.model_validate(
                result.model_dump(mode="python"), strict=True
            )
        except ValidationError:
            failures.append("validation_result_invalid")
        if result.validation_result_id in result_ids:
            failures.append("duplicate_validation_result")
        result_ids.add(result.validation_result_id)
        if result.canonical_hash != result.calculate_canonical_hash():
            failures.append("validation_result_hash_mismatch")
        if result.claim_id != claim.claim_id:
            failures.append("validation_claim_mismatch")
        if (
            result.validator_name,
            result.validator_version,
        ) != GOVERNANCE_FIELD_VALIDATOR_ID:
            failures.append("validator_contract_not_approved")
            continue
        approved_validator_seen = True
        if result.verification_status != VerificationStatus.PASSED:
            failures.append("validation_result_not_passed")
        for check in result.checks:
            if not check.passed:
                failures.append("validation_check_failed")
                continue
            passed_check_codes.add(check.check_code)
            if (
                evidence_span is not None
                and check.check_code == "evidence_span_bound"
                and evidence_span.evidence_span_id in check.evidence_span_ids
            ):
                evidence_binding_proven = True

    if not approved_validator_seen or not REQUIRED_AUTOMATIC_VALIDATION_CHECK_CODES.issubset(
        passed_check_codes
    ):
        failures.append("validation_results_incomplete")
    if not evidence_binding_proven:
        failures.append("validation_evidence_binding_missing")
    return tuple(dict.fromkeys(failures))


def admission_failures(
    claim: GovernanceClaim,
    *,
    evidence_span: GovernanceEvidenceSpan | None = None,
    validation_results: tuple[ValidationResult, ...] = (),
    has_unresolved_conflict: bool | None = None,
) -> tuple[str, ...]:
    """Return stable reason codes for deterministic automatic admission."""

    failures: list[str] = []
    if claim.source_role not in AUTHORITATIVE_SOURCE_ROLES:
        failures.append("source_not_authoritative")
    if not claim.raw_hash_verified:
        failures.append("raw_hash_unverified")
    if not claim.lineage_complete:
        failures.append("lineage_incomplete")
    if not claim.evidence_locator_complete:
        failures.append("evidence_locator_incomplete")
    if claim.extractor_kind != ExtractorKind.DETERMINISTIC:
        failures.append("extractor_not_deterministic")
    if claim.extraction_status != ExtractionStatus.COMPLETE:
        failures.append("extraction_not_complete")
    if claim.verification_status != VerificationStatus.PASSED:
        failures.append("verification_not_passed")
    if claim.review_status != ReviewStatus.NOT_REQUIRED:
        failures.append("review_not_not_required")
    conflict = (
        claim.has_unresolved_conflict
        if has_unresolved_conflict is None
        else has_unresolved_conflict
    )
    if conflict:
        failures.append("unresolved_conflict")

    failures.extend(
        _external_validation_failures(
            claim,
            evidence_span=evidence_span,
            validation_results=validation_results,
        )
    )

    return tuple(dict.fromkeys(failures))


def canonical_eligible(
    claim: GovernanceClaim,
    *,
    evidence_span: GovernanceEvidenceSpan | None = None,
    validation_results: tuple[ValidationResult, ...] = (),
    has_unresolved_conflict: bool | None = None,
) -> bool:
    """Pure automatic-admission predicate; callers cannot override its outcome."""

    return not admission_failures(
        claim,
        evidence_span=evidence_span,
        validation_results=validation_results,
        has_unresolved_conflict=has_unresolved_conflict,
    )


def reviewed_candidate_eligible(
    claim: GovernanceClaim,
    decision: ReviewDecision,
    *,
    evidence_span: GovernanceEvidenceSpan | None = None,
    validation_results: tuple[ValidationResult, ...] = (),
    has_unresolved_conflict: bool | None = None,
) -> bool:
    """Check the optional maintenance path without mutating the candidate claim."""

    if decision.candidate_claim_id != claim.claim_id:
        return False
    if decision.decision != DecisionValue.APPROVED:
        return False
    if claim.source_role not in AUTHORITATIVE_SOURCE_ROLES:
        return False
    if claim.review_status not in {ReviewStatus.PENDING, ReviewStatus.APPROVED}:
        return False
    if claim.extraction_status != ExtractionStatus.COMPLETE:
        return False
    if claim.verification_status != VerificationStatus.PASSED:
        return False
    if not (
        claim.raw_hash_verified
        and claim.lineage_complete
        and claim.evidence_locator_complete
    ):
        return False
    conflict = (
        claim.has_unresolved_conflict
        if has_unresolved_conflict is None
        else has_unresolved_conflict
    )
    if conflict:
        return False
    if (
        evidence_span is None
        or evidence_span.evidence_span_id not in decision.evidence_span_ids
    ):
        return False
    return not _external_validation_failures(
        claim,
        evidence_span=evidence_span,
        validation_results=validation_results,
    )


__all__ = [
    "AUTHORITATIVE_SOURCE_ROLES",
    "GOVERNANCE_FIELD_VALIDATOR_ID",
    "REQUIRED_AUTOMATIC_VALIDATION_CHECK_CODES",
    "admission_failures",
    "canonical_eligible",
    "reviewed_candidate_eligible",
]
