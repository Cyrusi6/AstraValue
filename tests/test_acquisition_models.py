from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from analysis.acquisition.models import (
    AcquisitionAttempt,
    AcquisitionAttemptEvent,
    AcquisitionAttemptEventType,
    AcquisitionExecutionLease,
    AcquisitionOutcome,
    AttemptKind,
    AvailableAtBasis,
    BarrierResolution,
    ContentBlob,
    EvidenceManifestItem,
    PhysicalQueryCoverageLink,
    PublishedAtPrecision,
    RawResourceSnapshot,
    ResourceRole,
    StorageBindingIntent,
)
from analysis.models import DocumentRecord, SourceRecord


NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
HASH = "a" * 64


def test_acquisition_outcome_is_exact_closed_set_and_excludes_abandoned():
    assert {item.value for item in AcquisitionOutcome} == {
        "success",
        "unchanged",
        "no_data",
        "restricted",
        "paywalled",
        "login_required",
        "rate_limited",
        "timeout",
        "network_failed",
        "parse_failed",
        "policy_skipped",
        "partial_success",
    }
    assert "abandoned" not in {item.value for item in AcquisitionOutcome}


def test_datetime_fields_require_timezone_and_normalize_to_utc():
    with pytest.raises(ValidationError, match="时区"):
        AcquisitionExecutionLease(
            run_id="run",
            owner_token_hash=HASH,
            lease_epoch=1,
            acquired_at=NOW.replace(tzinfo=None),
            heartbeat_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
    china = timezone(timedelta(hours=8))
    lease = AcquisitionExecutionLease(
        run_id="run",
        owner_token_hash=HASH,
        lease_epoch=2,
        acquired_at=NOW.astimezone(china),
        heartbeat_at=NOW.astimezone(china),
        expires_at=(NOW + timedelta(seconds=30)).astimezone(china),
    )
    assert lease.acquired_at == NOW
    assert lease.ttl == timedelta(seconds=30)
    assert lease.is_active_at(NOW + timedelta(seconds=1))


def test_acquisition_models_are_frozen_and_plan_link_identity_is_m2m_pair():
    link = PhysicalQueryCoverageLink(plan_item_id="plan-1", coverage_entry_id="coverage-1")
    assert link.identity == ("plan-1", "coverage-1")
    with pytest.raises(ValidationError):
        link.plan_item_id = "changed"


def test_attempt_references_only_physical_plan_and_validates_kind():
    attempt = AcquisitionAttempt(
        attempt_id="attempt-1",
        run_id="run-1",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        physical_query_plan_item_id="plan-1",
        execution_key="exec-1",
        attempt_kind=AttemptKind.DISCOVERY,
        query_id="cninfo.periodic_report",
        work_position="page:1",
        retry_group_id="retry-1",
        lease_epoch=1,
        started_at=NOW,
    )
    assert attempt.physical_query_plan_item_id == "plan-1"
    assert "coverage_entry_id" not in AcquisitionAttempt.model_fields
    with pytest.raises(ValidationError, match="父discovery"):
        AcquisitionAttempt(
            **attempt.model_dump(exclude={"attempt_kind", "query_id"}),
            attempt_kind="fetch",
        )


def test_outcome_terminal_and_abandoned_lifecycle_are_mutually_exclusive():
    terminal = AcquisitionAttemptEvent(
        attempt_id="attempt-1",
        event_type="outcome_terminal",
        occurred_at=NOW,
        lease_epoch=1,
        outcome="success",
    )
    assert terminal.outcome == AcquisitionOutcome.SUCCESS
    abandoned = AcquisitionAttemptEvent(
        attempt_id="attempt-1",
        event_type="abandoned",
        occurred_at=NOW,
        lease_epoch=1,
        reason_code="lease_expired",
    )
    assert abandoned.event_type == AcquisitionAttemptEventType.ABANDONED
    with pytest.raises(ValidationError, match="不得携带"):
        AcquisitionAttemptEvent(
            attempt_id="attempt-1",
            event_type="abandoned",
            occurred_at=NOW,
            lease_epoch=1,
            outcome="timeout",
            reason_code="process_exit",
        )


def test_barrier_resolution_requires_successor_and_proof_or_snapshot_observation():
    resolution = BarrierResolution(
        barrier_id="barrier-1",
        opening_attempt_id="attempt-1",
        resolving_attempt_id="attempt-2",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        partition_key="periodic_report",
        work_position="page:3",
        retry_group_id="retry-1",
        discovery_proof_id="proof-1",
        created_at=NOW,
        lease_epoch=2,
    )
    assert resolution.opening_attempt_id == "attempt-1"
    with pytest.raises(ValidationError, match="后继attempt"):
        BarrierResolution(
            **resolution.model_dump(
                exclude={"barrier_resolution_id", "resolving_attempt_id"}
            ),
            resolving_attempt_id="attempt-1",
        )


def test_storage_binding_intent_has_only_identity_hashes_not_absolute_paths():
    intent = StorageBindingIntent(
        namespace_id="namespace-1",
        binding_nonce="nonce-1",
        layout_version=1,
        database_identity_hash="b" * 64,
        data_root_identity_hash="c" * 64,
        source_database_fingerprint="d" * 64,
        source_user_version=5,
        bootstrap_stage="backup_v5_verified",
        stage_manifest_hashes={"backup_v5": "e" * 64},
        created_at=NOW,
        updated_at=NOW,
    )
    assert "path" not in " ".join(StorageBindingIntent.model_fields)
    with pytest.raises(ValidationError, match="Extra inputs"):
        StorageBindingIntent(
            **intent.model_dump(exclude={"created_at", "updated_at"}),
            created_at=NOW,
            updated_at=NOW,
            database_path="D:/private/analysis.db",
        )


def _snapshot_base() -> dict:
    return {
        "snapshot_id": "snapshot-1",
        "storage_namespace_id": "namespace-1",
        "source_definition_id": "cninfo.disclosures",
        "source_definition_version": "1.0.0",
        "creating_observation_id": "observation-1",
        "content_blob_id": "blob-1",
        "mime_type": "application/pdf",
        "byte_length": 3,
        "sha256": HASH,
        "archive_relative_path": f"raw/blobs/sha256/aa/{HASH}",
        "available_at": NOW,
        "version": 1,
        "policy_decision": "archive_allowed",
        "created_at": NOW,
    }


def test_raw_snapshot_content_and_discovery_response_condition_fields():
    content = RawResourceSnapshot(
        **_snapshot_base(),
        resource_role=ResourceRole.CONTENT,
        available_at_basis=AvailableAtBasis.RETRIEVED_AT,
        canonical_resource_id="announcement-1",
        upstream_material_id="announcement-1",
        canonical_url="https://static.cninfo.com.cn/report.pdf",
        published_at_raw="2020-01-01",
        published_at=NOW - timedelta(days=1),
        published_at_precision=PublishedAtPrecision.DATE,
        source_timezone="Asia/Shanghai",
    )
    assert content.resource_role == ResourceRole.CONTENT
    discovery_base = _snapshot_base()
    discovery_base["mime_type"] = "application/json"
    discovery = RawResourceSnapshot(
        **discovery_base,
        resource_role=ResourceRole.DISCOVERY_RESPONSE,
        available_at_basis=AvailableAtBasis.RETRIEVED_AT,
        physical_query_plan_item_id="plan-1",
        page_number=1,
        query_page_canonical="plan-1:page:1",
    )
    assert discovery.upstream_material_id is None
    with pytest.raises(ValidationError, match="不得伪造"):
        RawResourceSnapshot(
            **discovery.model_dump(exclude={"canonical_resource_id"}),
            canonical_resource_id="fake-announcement",
        )


def test_full_sha256_relative_blob_and_manifest_derived_hash_pairing():
    blob = ContentBlob(
        content_blob_id="blob-1",
        storage_namespace_id="namespace-1",
        sha256=HASH,
        byte_length=3,
        archive_relative_path=f"raw/blobs/sha256/aa/{HASH}",
        created_at=NOW,
    )
    assert blob.sha256 == HASH
    with pytest.raises(ValidationError, match="一一对应"):
        EvidenceManifestItem(
            snapshot_id="snapshot-1",
            derived_artifact_ids=("derived-1",),
            source_definition_id="cninfo.disclosures",
            source_definition_version="1.0.0",
            snapshot_sha256=HASH,
            available_at=NOW,
        )


def test_legacy_models_expose_optional_snapshot_compatibility_fields():
    source = SourceRecord(name="old")
    assert source.raw_resource_snapshot_id is None
    document = DocumentRecord(
        ticker="600519",
        title="fixture",
        archived_path="raw/a.pdf",
        text_path="raw/a.txt",
        sha256=HASH,
        source=source,
    )
    assert document.document_version == 1
    assert document.raw_resource_snapshot_id is None
