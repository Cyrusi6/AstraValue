from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from analysis.governance.models import (
    ClaimObjectType,
    CompletenessStatus,
    DeltaDisposition,
    ExtractionStatus,
    ExtractorKind,
    GovernanceClaim,
    GovernanceEvidenceSpan,
    GovernancePerspective,
    GovernanceQuestionCoverageLink,
    OwnershipPosition,
    OwnershipSnapshot,
    ReviewStatus,
    RoleTenure,
    SourceRole,
    TimePrecision,
    VerificationStatus,
)
from analysis.governance.snapshot_service import (
    GovernanceSnapshotService,
    SnapshotAnchorInput,
    SnapshotBuildRequest,
    SnapshotDeltaInput,
    SnapshotIntegrityError,
    canonical_governance_snapshot_hash,
    governance_snapshot_semantic_payload,
    validate_governance_snapshot_semantic_hash,
)


T0 = datetime(2022, 12, 31, 8, tzinfo=timezone.utc)
T1 = datetime(2023, 6, 1, 8, tzinfo=timezone.utc)
T2 = datetime(2024, 2, 1, 8, tzinfo=timezone.utc)
T3 = T2 + timedelta(days=1)
COMPANY = "company:600519"
QUESTION = "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND"
OWNERSHIP_QUESTION = "GOV.Q01.OWNERSHIP_CONTROL"
ZERO = "0" * 64
CONTENT_HASH = "1" * 64
EXCERPT_HASH = "2" * 64
MANIFEST_HASH = "3" * 64


def with_hash(value: object, field_name: str = "canonical_hash") -> object:
    digest = value.calculate_canonical_hash(hash_field=field_name)  # type: ignore[attr-defined]
    return value.model_copy(update={field_name: digest})  # type: ignore[attr-defined]


def evidence_span() -> GovernanceEvidenceSpan:
    return GovernanceEvidenceSpan(
        evidence_span_id="govspan:appointment",
        raw_snapshot_id="raw:appointment",
        content_hash=CONTENT_HASH,
        page=10,
        paragraph_id="paragraph:1",
        excerpt_hash=EXCERPT_HASH,
    )


def claim(*, available_at: datetime = T1) -> GovernanceClaim:
    provisional = GovernanceClaim(
        claim_id="govclaim:appointment",
        extraction_run_id="govxrun:1",
        company_id=COMPANY,
        question_id=QUESTION,
        subject_type="person",
        subject_id=f"govp:{COMPANY}:p1",
        predicate="appointed_as",
        object_type=ClaimObjectType.TEXT,
        object_value="总经理",
        record_type="role_tenure",
        reference_at=T1,
        effective_at=T1,
        valid_from=T1,
        announced_at=T1,
        available_at=available_at,
        retrieved_at=available_at,
        time_precision=TimePrecision.DATETIME,
        source_role=SourceRole.OFFICIAL_DISCLOSURE,
        evidence_manifest_id="shared-manifest:1",
        raw_snapshot_id="raw:appointment",
        content_hash=CONTENT_HASH,
        evidence_span_id="govspan:appointment",
        upstream_material_id="upstream:appointment",
        independence_group="upstream:appointment",
        extractor_kind=ExtractorKind.DETERMINISTIC,
        extractor_version="role-table.v1",
        extraction_status=ExtractionStatus.COMPLETE,
        verification_status=VerificationStatus.PASSED,
        review_status=ReviewStatus.NOT_REQUIRED,
        raw_hash_verified=True,
        lineage_complete=True,
        evidence_locator_complete=True,
        canonical_hash=ZERO,
    )
    return with_hash(provisional)  # type: ignore[return-value]


def role_record(*, canonical_hash: str | None = None) -> RoleTenure:
    provisional = RoleTenure(
        record_id="govrec:role-tenure-1",
        company_id=COMPANY,
        question_id=QUESTION,
        claim_ids=("govclaim:appointment",),
        evidence_span_ids=("govspan:appointment",),
        source_event_ids=("legacy:event:appointment",),
        reference_at=T1,
        effective_at=T1,
        valid_from=T1,
        available_at=T1,
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=ZERO,
        person_id=f"govp:{COMPANY}:p1",
        role_code="general_manager",
        original_role_text="总经理",
        role_scope="listed_company",
        appointment_event_id="legacy:event:appointment",
        appointment_evidence_span_id="govspan:appointment",
    )
    if canonical_hash is not None:
        return provisional.model_copy(update={"canonical_hash": canonical_hash})
    return with_hash(provisional)  # type: ignore[return-value]


def build_request(**updates: object) -> SnapshotBuildRequest:
    record = role_record()
    base = SnapshotBuildRequest(
        namespace="pilot:governance",
        company_id=COMPANY,
        state_at=T2,
        known_at=T2,
        perspective=GovernancePerspective.STRICT,
        question_set_version="1.0.0",
        source_registry_version="1.0.0",
        query_pack_version="1.0.0",
        extractor_versions=("role-table.v1",),
        reconstruction_version="1.0.0",
        evidence_manifest_id="shared-manifest:1",
        evidence_manifest_hash=MANIFEST_HASH,
        actual_manifest_hash=MANIFEST_HASH,
        canonical_records=(record,),
        claims=(claim(),),
        evidence_spans=(evidence_span(),),
        question_level_coverage=(
            GovernanceQuestionCoverageLink(
                question_id=QUESTION,
                coverage_entry_ids=("shared-coverage:1",),
                completeness_status=CompletenessStatus.COMPLETE,
            ),
        ),
        anchors=(
            SnapshotAnchorInput(
                question_id=QUESTION,
                state_kind="roles",
                anchor_record_id=record.record_id,
                reference_at=T1,
                available_at=T1,
            ),
        ),
        deltas=(
            SnapshotDeltaInput(
                question_id=QUESTION,
                delta_record_id=record.record_id,
                disposition=DeltaDisposition.APPLIED,
                effective_at=T1,
                announced_at=T1,
                available_at=T1,
            ),
        ),
        completeness_status=CompletenessStatus.COMPLETE,
        created_at=T2,
    )
    return replace(base, **updates)


def test_deterministic_hash_and_idempotent_creation() -> None:
    service = GovernanceSnapshotService(namespace="pilot:governance")
    first = service.create(build_request())
    second = service.create(build_request(created_at=T3))
    assert first.idempotent_reuse is False
    assert second.idempotent_reuse is True
    assert second.snapshot is first.snapshot
    assert first.snapshot.governance_snapshot_id.endswith(
        first.snapshot.canonical_snapshot_hash
    )
    assert service.get_snapshot(first.snapshot.governance_snapshot_id) is first.snapshot


def test_public_semantic_hash_helpers_validate_frozen_preimage_and_tampering() -> None:
    request = build_request()
    semantic_payload = governance_snapshot_semantic_payload(request)
    expected_hash = canonical_governance_snapshot_hash(semantic_payload)
    snapshot = GovernanceSnapshotService(namespace="pilot:governance").create(
        request
    ).snapshot

    assert snapshot.canonical_snapshot_hash == expected_hash
    assert snapshot.governance_snapshot_id == f"govsnapshot:{expected_hash}"
    validate_governance_snapshot_semantic_hash(snapshot, semantic_payload)

    tampered_snapshot = snapshot.model_copy(update={"query_pack_version": "tampered"})
    with pytest.raises(SnapshotIntegrityError) as snapshot_error:
        validate_governance_snapshot_semantic_hash(
            tampered_snapshot,
            semantic_payload,
        )
    assert snapshot_error.value.code == "snapshot_semantic_mismatch"

    tampered_preimage = dict(semantic_payload)
    tampered_preimage["namespace"] = "another:namespace"
    with pytest.raises(SnapshotIntegrityError) as preimage_error:
        validate_governance_snapshot_semantic_hash(snapshot, tampered_preimage)
    assert preimage_error.value.code == "snapshot_hash_mismatch"


def test_new_rules_create_new_snapshot_and_old_snapshot_is_immutable() -> None:
    service = GovernanceSnapshotService(namespace="pilot:governance")
    first = service.create(build_request()).snapshot
    old_bytes = first.canonical_bytes()
    second = service.create(
        build_request(
            reconstruction_version="1.0.1",
            supersedes_snapshot_id=first.governance_snapshot_id,
        )
    ).snapshot
    assert second.governance_snapshot_id != first.governance_snapshot_id
    assert second.supersedes_snapshot_id == first.governance_snapshot_id
    assert service.get_snapshot(first.governance_snapshot_id).canonical_bytes() == old_bytes


def test_future_leak_fails_closed() -> None:
    service = GovernanceSnapshotService(namespace="pilot:governance")
    future_claim = claim(available_at=T3)
    with pytest.raises(SnapshotIntegrityError) as error:
        service.create(build_request(claims=(future_claim,)))
    assert error.value.code == "future_leak"


def test_broken_lineage_fails_closed() -> None:
    service = GovernanceSnapshotService(namespace="pilot:governance")
    with pytest.raises(SnapshotIntegrityError) as error:
        service.create(build_request(claims=()))
    assert error.value.code == "broken_lineage"


def test_cross_scope_and_cross_namespace_fail_closed() -> None:
    service = GovernanceSnapshotService(namespace="pilot:governance")
    with pytest.raises(SnapshotIntegrityError) as namespace_error:
        service.create(build_request(namespace="another:namespace"))
    assert namespace_error.value.code == "cross_namespace"
    with pytest.raises(SnapshotIntegrityError) as scope_error:
        service.create(build_request(acquisition_scope="business_model"))
    assert scope_error.value.code == "cross_scope"


def test_hash_mismatch_for_record_and_manifest_fails_closed() -> None:
    service = GovernanceSnapshotService(namespace="pilot:governance")
    with pytest.raises(SnapshotIntegrityError) as record_error:
        service.create(build_request(canonical_records=(role_record(canonical_hash=ZERO),)))
    assert record_error.value.code == "hash_mismatch"
    with pytest.raises(SnapshotIntegrityError) as manifest_error:
        service.create(build_request(actual_manifest_hash="4" * 64))
    assert manifest_error.value.code == "manifest_hash_mismatch"

    with pytest.raises(SnapshotIntegrityError) as missing_manifest_error:
        service.create(build_request(actual_manifest_hash=None))  # type: ignore[arg-type]
    assert missing_manifest_error.value.code == "manifest_hash_mismatch"


def test_nested_ownership_position_hash_mismatch_fails_closed() -> None:
    service = GovernanceSnapshotService(namespace="pilot:governance")
    ownership_claim_payload = claim().model_dump(mode="python")
    ownership_claim_payload.update(
        claim_id="govclaim:ownership",
        question_id=OWNERSHIP_QUESTION,
        subject_type="company",
        subject_id=COMPANY,
        predicate="ownership_snapshot",
        record_type="ownership_snapshot",
        target_record_id="govrec:ownership",
    )
    ownership_claim = GovernanceClaim.model_validate_with_hash(
        ownership_claim_payload
    )
    position = OwnershipPosition.model_validate_with_hash(
        {
            "position_id": "govposition:ownership",
            "company_id": COMPANY,
            "holder_entity_id": "entity:holder",
            "holder_name": "控股股东",
            "share_count": "100",
            "ratio": "0.5",
            "ratio_basis": "200",
            "share_class": "A",
            "capital_basis": "total_issued_shares",
            "completeness_status": CompletenessStatus.COMPLETE,
            "claim_ids": (ownership_claim.claim_id,),
            "evidence_span_ids": ("govspan:appointment",),
        }
    )
    ownership = OwnershipSnapshot.model_validate_with_hash(
        {
            "record_id": "govrec:ownership",
            "company_id": COMPANY,
            "question_id": OWNERSHIP_QUESTION,
            "claim_ids": (ownership_claim.claim_id,),
            "evidence_span_ids": ("govspan:appointment",),
            "reference_at": T1,
            "available_at": T1,
            "completeness_status": CompletenessStatus.COMPLETE,
            "positions": (position,),
            "total_share_basis": "200",
            "capital_basis": "total_issued_shares",
            "is_complete": True,
            "source_manifest_id": "shared-manifest:1",
        }
    )
    tampered_position = position.model_copy(update={"canonical_hash": ZERO})
    tampered_payload = ownership.model_dump(mode="python")
    tampered_payload["positions"] = (tampered_position,)
    tampered = OwnershipSnapshot.model_validate_with_hash(tampered_payload)
    assert tampered.canonical_hash == tampered.calculate_canonical_hash()
    assert (
        tampered.positions[0].canonical_hash
        != tampered.positions[0].calculate_canonical_hash()
    )
    request = build_request(
        canonical_records=(tampered,),
        claims=(ownership_claim,),
        anchors=(
            SnapshotAnchorInput(
                question_id=OWNERSHIP_QUESTION,
                state_kind="ownership",
                anchor_record_id=tampered.record_id,
                reference_at=T1,
                available_at=T1,
            ),
        ),
        deltas=(),
        question_level_coverage=(
            GovernanceQuestionCoverageLink(
                question_id=OWNERSHIP_QUESTION,
                coverage_entry_ids=("shared-coverage:ownership",),
                completeness_status=CompletenessStatus.COMPLETE,
            ),
        ),
    )

    with pytest.raises(SnapshotIntegrityError) as error:
        service.create(request)

    assert error.value.code == "hash_mismatch"


def test_business_incomplete_state_is_publishable_without_manual_gate() -> None:
    service = GovernanceSnapshotService(namespace="pilot:governance")
    request = build_request(
        canonical_records=(),
        claims=(),
        evidence_spans=(),
        anchors=(),
        deltas=(),
        active_gap_ids=("govgap:missing-anchor",),
        completeness_status=CompletenessStatus.INCOMPLETE,
        question_level_coverage=(
            GovernanceQuestionCoverageLink(
                question_id=QUESTION,
                coverage_entry_ids=("shared-coverage:partial",),
                completeness_status=CompletenessStatus.INCOMPLETE,
            ),
        ),
    )
    result = service.create(request)
    assert result.snapshot.completeness_status == CompletenessStatus.INCOMPLETE
    assert result.snapshot.active_gap_ids == ("govgap:missing-anchor",)
