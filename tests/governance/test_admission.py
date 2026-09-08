from __future__ import annotations

from datetime import timezone

import pytest

from analysis.governance.admission import (
    REQUIRED_AUTOMATIC_VALIDATION_CHECK_CODES,
    admission_failures,
    canonical_eligible,
    reviewed_candidate_eligible,
)
from analysis.governance.extraction import CandidateBoundaryError, CandidateExtractor
from analysis.governance.models import (
    DecisionValue,
    ExtractionStatus,
    ExtractorKind,
    GovernanceEvidenceSpan,
    ReviewDecision,
    ReviewStatus,
    SourceRole,
    ValidationCheck,
    ValidationResult,
    VerificationStatus,
)
from tests.governance.test_models import H, H2, SPAN_ID, T1, T2, valid_claim


def valid_span() -> GovernanceEvidenceSpan:
    return GovernanceEvidenceSpan(
        evidence_span_id=SPAN_ID,
        raw_snapshot_id="shared-raw:1",
        content_hash=H,
        page=1,
        paragraph_id="paragraph:1",
        char_start=0,
        char_end=10,
        excerpt_hash=H2,
    )


def valid_result() -> ValidationResult:
    checks = tuple(
        ValidationCheck(
            check_code=code,
            passed=True,
            evidence_span_ids=(SPAN_ID,) if code == "evidence_span_bound" else (),
        )
        for code in sorted(REQUIRED_AUTOMATIC_VALIDATION_CHECK_CODES)
    )
    return ValidationResult.model_validate_with_hash(
        {
            "validation_result_id": "govvalidation:1",
            "claim_id": "govclaim:1",
            "validator_name": "governance-field-validator",
            "validator_version": "1.0.0",
            "verification_status": VerificationStatus.PASSED,
            "checks": checks,
            "validated_at": T2,
        }
    )


def review_for_claim() -> ReviewDecision:
    return ReviewDecision(
        review_decision_id="govreview:1",
        candidate_claim_id="govclaim:1",
        decision=DecisionValue.APPROVED,
        decided_by="maintainer:1",
        rationale="compared against formal original",
        evidence_span_ids=(SPAN_ID,),
        decided_at=T1,
        available_at=T2,
        canonical_hash=H,
    )


def test_canonical_eligible_requires_every_automatic_admission_gate() -> None:
    claim = valid_claim()
    assert canonical_eligible(
        claim,
        evidence_span=valid_span(),
        validation_results=(valid_result(),),
    )
    mutations = (
        {"source_role": SourceRole.DISCOVERY_ONLY},
        {"raw_hash_verified": False},
        {"lineage_complete": False},
        {"evidence_locator_complete": False},
        {"extractor_kind": ExtractorKind.REGEX},
        {"extraction_status": ExtractionStatus.PARTIAL},
        {"verification_status": VerificationStatus.FAILED},
        {"review_status": ReviewStatus.PENDING},
        {"has_unresolved_conflict": True},
    )
    for mutation in mutations:
        assert not canonical_eligible(
            valid_claim(**mutation),
            evidence_span=valid_span(),
            validation_results=(valid_result(),),
        ), mutation


def test_public_admission_path_rejects_claim_self_attestation() -> None:
    claim = valid_claim()

    failures = admission_failures(claim)

    assert "evidence_span_missing" in failures
    assert "validation_results_missing" in failures
    assert not canonical_eligible(claim)
    assert claim.canonical_eligible is False


def test_admission_rejects_incomplete_or_tampered_validator_results() -> None:
    claim = valid_claim()
    incomplete = ValidationResult.model_validate_with_hash(
        {
            "validation_result_id": "govvalidation:incomplete",
            "claim_id": claim.claim_id,
            "validator_name": "governance-field-validator",
            "validator_version": "1.0.0",
            "verification_status": VerificationStatus.PASSED,
            "checks": (
                ValidationCheck(check_code="field_type_valid", passed=True),
            ),
            "validated_at": T2,
        }
    )
    failures = admission_failures(
        claim,
        evidence_span=valid_span(),
        validation_results=(incomplete,),
    )
    assert "validation_results_incomplete" in failures
    assert "validation_evidence_binding_missing" in failures
    assert not canonical_eligible(
        claim,
        evidence_span=valid_span(),
        validation_results=(incomplete,),
    )

    tampered = valid_result().model_copy(update={"canonical_hash": H})
    assert "validation_result_hash_mismatch" in admission_failures(
        claim,
        evidence_span=valid_span(),
        validation_results=(tampered,),
    )


def test_admission_checks_bound_evidence_hash_and_lineage() -> None:
    claim = valid_claim()
    wrong_span = GovernanceEvidenceSpan(
        **{
            **valid_span().model_dump(),
            "raw_snapshot_id": "shared-raw:other",
        }
    )
    failures = admission_failures(
        claim,
        evidence_span=wrong_span,
        validation_results=(valid_result(),),
    )
    assert "raw_snapshot_mismatch" in failures
    assert not canonical_eligible(
        claim,
        evidence_span=wrong_span,
        validation_results=(valid_result(),),
    )

    malformed_span = valid_span().model_copy(update={"excerpt_hash": "not-a-hash"})
    assert "evidence_span_invalid" in admission_failures(
        claim,
        evidence_span=malformed_span,
        validation_results=(valid_result(),),
    )


def test_A16_llm_candidate_can_only_use_separate_append_only_review_path() -> None:
    candidate = valid_claim(
        extractor_kind=ExtractorKind.LLM,
        review_status=ReviewStatus.PENDING,
    )
    original_bytes = candidate.canonical_bytes()
    decision = review_for_claim()

    assert not canonical_eligible(candidate)
    assert reviewed_candidate_eligible(
        candidate,
        decision,
        evidence_span=valid_span(),
        validation_results=(valid_result(),),
    )
    assert candidate.canonical_bytes() == original_bytes
    assert candidate.review_status == ReviewStatus.PENDING


def test_A13_equal_values_never_upgrade_nonformal_sources() -> None:
    formal_roles = (
        SourceRole.OFFICIAL_DISCLOSURE,
        SourceRole.REGULATOR_EXCHANGE,
        SourceRole.STRUCTURED_SUPPLIER,
    )
    nonformal_roles = (
        SourceRole.DISCOVERY_ONLY,
        SourceRole.CONTEXTUAL_EVIDENCE,
        SourceRole.DEFERRED,
    )
    claims = {
        role: valid_claim(source_role=role)
        for role in (*formal_roles, *nonformal_roles)
    }
    # All candidates carry the same typed value, evidence and validation.  The
    # source role remains an independent, non-overridable admission gate.
    assert len({claim.object_value for claim in claims.values()}) == 1
    assert all(
        canonical_eligible(
            claims[role],
            evidence_span=valid_span(),
            validation_results=(valid_result(),),
        )
        for role in formal_roles
    )
    assert all(
        not canonical_eligible(
            claims[role],
            evidence_span=valid_span(),
            validation_results=(valid_result(),),
        )
        for role in nonformal_roles
    )


def test_structured_supplier_requires_external_row_field_validation() -> None:
    claim = valid_claim(source_role=SourceRole.STRUCTURED_SUPPLIER)
    span = GovernanceEvidenceSpan(
        evidence_span_id=SPAN_ID,
        raw_snapshot_id="shared-raw:1",
        content_hash=H,
        table_id="RPT_F10_ORGINFO_MANAINTRO",
        row_label="PERSON_CODE=person:1;REPORT_DATE=2025-12-31",
        column_label="POSITION",
        excerpt_hash=H2,
    )

    assert canonical_eligible(
        claim,
        evidence_span=span,
        validation_results=(valid_result(),),
    )
    assert not canonical_eligible(claim, evidence_span=span)


@pytest.mark.parametrize(
    "role",
    [
        SourceRole.DISCOVERY_ONLY,
        SourceRole.CONTEXTUAL_EVIDENCE,
        SourceRole.DEFERRED,
    ],
)
def test_review_cannot_upgrade_nonformal_source(role: SourceRole) -> None:
    candidate = valid_claim(
        source_role=role,
        extractor_kind=ExtractorKind.LLM,
        review_status=ReviewStatus.PENDING,
    )
    assert not reviewed_candidate_eligible(
        candidate,
        review_for_claim(),
        evidence_span=valid_span(),
        validation_results=(valid_result(),),
    )


def test_conflict_override_fails_closed_even_if_claim_flag_is_false() -> None:
    claim = valid_claim()
    assert canonical_eligible(
        claim,
        evidence_span=valid_span(),
        validation_results=(valid_result(),),
    )
    assert not canonical_eligible(
        claim,
        evidence_span=valid_span(),
        validation_results=(valid_result(),),
        has_unresolved_conflict=True,
    )


def test_ambiguous_regex_is_forced_through_candidate_boundary() -> None:
    extractor = CandidateExtractor(
        extractor_kind=ExtractorKind.REGEX,
        name="ambiguous-resignation-regex",
        version="1",
        ambiguous=True,
    )
    candidate = extractor.isolate_claim(valid_claim())
    assert candidate.extractor_kind == ExtractorKind.REGEX
    assert candidate.review_status == ReviewStatus.PENDING
    assert not canonical_eligible(candidate)

    with pytest.raises(CandidateBoundaryError):
        CandidateExtractor(
            extractor_kind=ExtractorKind.REGEX,
            name="unambiguous-regex",
            version="1",
            ambiguous=False,
        )


def test_optional_review_is_separate_and_report_runner_cannot_approve() -> None:
    extractor = CandidateExtractor(
        extractor_kind=ExtractorKind.LLM,
        name="llm-governance",
        version="model-1",
    )
    assert not hasattr(extractor, "approve")
    assert not hasattr(extractor, "write_review_decision")

    candidate = extractor.isolate_claim(valid_claim())
    original = candidate.canonical_bytes()
    assert not reviewed_candidate_eligible(candidate, review_for_claim())
    assert reviewed_candidate_eligible(
        candidate,
        review_for_claim(),
        evidence_span=valid_span(),
        validation_results=(valid_result(),),
    )
    assert candidate.canonical_bytes() == original
    assert candidate.review_status == ReviewStatus.PENDING
