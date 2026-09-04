from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from analysis.acquisition.manifests import EvidenceManifestService
from analysis.acquisition.models import (
    AcquisitionAttempt,
    DiscoveredResource,
    DiscoveryObservation,
    DiscoveryProof,
    PhysicalQueryPlanItem,
)
from analysis.acquisition.snapshots import (
    ContentAddressedBlobStore,
    ContentSnapshotRequest,
    DiscoverySnapshotRequest,
    SnapshotCommitError,
    SnapshotService,
)


def _prepare_lineage(store):
    repository = store.repository
    _, token = repository.claim_lease(
        store.run.run_id, owner_token="snapshot-owner", now=store.now
    )
    discovery_attempt = AcquisitionAttempt(
        attempt_id="attempt-discovery-storage",
        run_id=store.run.run_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        physical_query_plan_item_id=store.plan_item.plan_item_id,
        execution_key=store.plan_item.execution_key,
        attempt_kind="discovery",
        query_id=store.plan_item.query_id,
        time_start=store.plan_item.time_start,
        time_end=store.plan_item.time_end,
        page_number=1,
        work_position="page:1",
        retry_group_id="retry-discovery-storage",
        lease_epoch=1,
        started_at=store.now,
    )
    repository.save_attempt(discovery_attempt, owner_token=token)
    response_hash = "a" * 64
    discovery_observation = DiscoveryObservation(
        observation_id="observation-discovery-lineage",
        attempt_id=discovery_attempt.attempt_id,
        physical_query_plan_item_id=store.plan_item.plan_item_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        page_number=1,
        observed_at=store.now,
        retrieved_at=store.now + timedelta(milliseconds=1),
        http_status=200,
        mime_type="application/json",
        response_sha256=response_hash,
        response_byte_length=2,
    )
    proof = DiscoveryProof(
        proof_id="proof-discovery-lineage",
        observation_id=discovery_observation.observation_id,
        attempt_id=discovery_attempt.attempt_id,
        physical_query_plan_item_id=store.plan_item.plan_item_id,
        response_sha256=response_hash,
        response_byte_length=2,
        http_status=200,
        mime_type="application/json",
        parser_id="fixture-parser",
        parser_version="1",
        schema_id="fixture-schema",
        schema_version="1",
        schema_valid=True,
        page_number=1,
        declared_total=1,
        declared_page_count=1,
        normalized_row_count=1,
        terminal=True,
        body_retained=False,
        replayable=False,
        created_at=store.now + timedelta(milliseconds=1),
    )
    resource = DiscoveredResource(
        discovered_resource_id="resource-storage-1",
        proof_id=proof.proof_id,
        discovery_observation_id=discovery_observation.observation_id,
        discovery_attempt_id=discovery_attempt.attempt_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        canonical_resource_id="cninfo:announcement:1",
        upstream_material_id="announcement-1",
        resource_url="https://static.cninfo.com.cn/finalpage/fixture.pdf",
        title="fixture annual report",
        published_at_raw="2026-09-01T08:00:00Z",
        published_at=store.now - timedelta(days=2),
        published_at_precision="instant",
        source_timezone="Asia/Shanghai",
        page_number=1,
        row_locator="rows[0]",
        row_hash="b" * 64,
        required_fetch=True,
        expected_mime_types=("application/pdf",),
    )
    repository.commit_discovery_bundle(
        discovery_observation,
        proof,
        (resource,),
        owner_token=token,
        lease_epoch=1,
    )
    fetch_plan = PhysicalQueryPlanItem(
        plan_item_id="plan-fetch-storage",
        run_id=store.run.run_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        query_id=f"{store.plan_item.query_id}.fetch",
        query_family=store.plan_item.query_family,
        execution_key="execution-fetch-storage",
        attempt_kind="fetch",
        request_method="GET",
        endpoint=resource.resource_url,
        normalized_parameters={},
        partition_key=store.plan_item.partition_key,
        pagination_fingerprint="single-resource-v1",
        ordinal=1,
        parent_plan_item_id=store.plan_item.plan_item_id,
        discovered_resource_id=resource.discovered_resource_id,
        time_start=store.plan_item.time_start,
        time_end=store.plan_item.time_end,
    )
    repository.save_plan_item(fetch_plan)
    fetch_attempt = AcquisitionAttempt(
        attempt_id="attempt-fetch-storage",
        run_id=store.run.run_id,
        source_definition_id=fetch_plan.source_definition_id,
        source_definition_version=fetch_plan.source_definition_version,
        physical_query_plan_item_id=fetch_plan.plan_item_id,
        execution_key=fetch_plan.execution_key,
        attempt_kind="fetch",
        discovered_resource_id=resource.discovered_resource_id,
        parent_discovery_attempt_id=discovery_attempt.attempt_id,
        time_start=fetch_plan.time_start,
        time_end=fetch_plan.time_end,
        work_position="resource:cninfo:announcement:1",
        retry_group_id="retry-fetch-storage",
        lease_epoch=1,
        started_at=store.now + timedelta(seconds=1),
    )
    repository.save_attempt(fetch_attempt, owner_token=token)
    blob_store = ContentAddressedBlobStore(
        store.data_root, store.namespace
    )
    service = SnapshotService(blob_store, repository)
    return {
        "token": token,
        "discovery_attempt": discovery_attempt,
        "discovery_observation": discovery_observation,
        "proof": proof,
        "resource": resource,
        "fetch_plan": fetch_plan,
        "fetch_attempt": fetch_attempt,
        "blob_store": blob_store,
        "service": service,
    }


def _content_request(store, lineage, *, observation_id):
    return ContentSnapshotRequest(
        attempt_id=lineage["fetch_attempt"].attempt_id,
        discovered_resource_id=lineage["resource"].discovered_resource_id,
        parent_discovery_attempt_id=lineage["discovery_attempt"].attempt_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        canonical_resource_id=lineage["resource"].canonical_resource_id,
        upstream_material_id=lineage["resource"].upstream_material_id,
        canonical_url=lineage["resource"].resource_url,
        mime_type="application/pdf",
        observed_at=store.now + timedelta(seconds=2),
        retrieved_at=store.now + timedelta(seconds=3),
        published_at_raw=lineage["resource"].published_at_raw,
        published_at=lineage["resource"].published_at,
        published_at_precision=lineage["resource"].published_at_precision,
        source_timezone=lineage["resource"].source_timezone,
        immutable_version_proven=True,
        policy_decision="allowed",
        original_url=lineage["resource"].resource_url,
        final_url=lineage["resource"].resource_url,
        http_status=200,
        observation_id=observation_id,
    )


def test_discovery_row_lineage_and_same_hash_observation_are_persisted(acquisition_store):
    store = acquisition_store
    lineage = _prepare_lineage(store)
    service = lineage["service"]
    request = DiscoverySnapshotRequest(
        attempt_id=lineage["discovery_attempt"].attempt_id,
        physical_query_plan_item_id=store.plan_item.plan_item_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        query_page_canonical="plan-storage-1:page:1",
        mime_type="application/json",
        observed_at=store.now + timedelta(seconds=4),
        retrieved_at=store.now + timedelta(seconds=5),
        page_number=1,
        observation_id="observation-discovery-snapshot-1",
    )
    first = service.freeze_discovery_response(
        b"{}", request, owner_token=lineage["token"], lease_epoch=1
    )
    repeated_request = replace(
        request,
        observed_at=store.now + timedelta(seconds=6),
        retrieved_at=store.now + timedelta(seconds=7),
        observation_id="observation-discovery-snapshot-2",
    )
    repeated = service.freeze_discovery_response(
        b"{}", repeated_request, owner_token=lineage["token"], lease_epoch=1
    )
    assert first.created_snapshot is True
    assert repeated.created_snapshot is False
    assert repeated.snapshot == first.snapshot
    observations = store.repository.list_discovery_observations(
        lineage["discovery_attempt"].attempt_id
    )
    assert {item.observation_id for item in observations} == {
        lineage["discovery_observation"].observation_id,
        "observation-discovery-snapshot-1",
        "observation-discovery-snapshot-2",
    }
    assert store.repository.get_discovery_proof(lineage["proof"].proof_id) == lineage[
        "proof"
    ]
    assert store.repository.list_discovered_resources(
        lineage["discovery_observation"].observation_id
    ) == [lineage["resource"]]


def test_content_snapshot_new_changed_unchanged_and_old_version_are_immutable(
    acquisition_store,
):
    store = acquisition_store
    lineage = _prepare_lineage(store)
    service = lineage["service"]
    first = service.freeze_content(
        b"version-one",
        _content_request(store, lineage, observation_id="resource-observation-1"),
        owner_token=lineage["token"],
        lease_epoch=1,
    )
    repeated = service.freeze_content(
        b"version-one",
        _content_request(store, lineage, observation_id="resource-observation-2"),
        owner_token=lineage["token"],
        lease_epoch=1,
    )
    changed = service.freeze_content(
        b"version-two",
        _content_request(store, lineage, observation_id="resource-observation-3"),
        owner_token=lineage["token"],
        lease_epoch=1,
    )
    assert first.disposition == "new"
    assert repeated.disposition == "unchanged"
    assert repeated.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert changed.disposition == "changed"
    assert changed.snapshot.version == 2
    assert changed.snapshot.supersedes_snapshot_id == first.snapshot.snapshot_id
    assert store.repository.get_raw_resource_snapshot(first.snapshot.snapshot_id) == first.snapshot
    assert store.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        canonical_resource_id=lineage["resource"].canonical_resource_id,
    ) == changed.snapshot
    observations = store.repository.list_resource_observations(
        attempt_id=lineage["fetch_attempt"].attempt_id
    )
    assert [item.disposition.value for item in observations] == [
        "new",
        "unchanged",
        "changed",
    ]


def test_derived_artifact_and_manifest_use_real_repository_field_contracts(
    acquisition_store,
):
    store = acquisition_store
    lineage = _prepare_lineage(store)
    frozen = lineage["service"].freeze_content(
        b"manifest-source",
        _content_request(store, lineage, observation_id="resource-observation-manifest"),
        owner_token=lineage["token"],
        lease_epoch=1,
    )
    artifact = lineage["service"].freeze_derived_artifact(
        parent_snapshot_id=frozen.snapshot.snapshot_id,
        artifact_type="text",
        extractor_id="existing-text-extractor",
        extractor_version="1.0.0",
        parameters={"layout": "plain"},
        output=b"derived text",
    )
    assert store.repository.get_derived_artifact(artifact.derived_artifact_id) == artifact
    assert store.repository.list_derived_artifacts(frozen.snapshot.snapshot_id) == [
        artifact
    ]
    manifest_service = EvidenceManifestService(
            lineage["blob_store"],
            store.repository,
            lambda source_id, version: {
                "policy_status": "enabled",
                "enabled": True,
                "access_method": "https_api",
                "live_access_review": {"status": "approved"},
                "license_policy": {
                    "llm_processing": "allowed",
                    "archive_original": "allowed",
            },
            "retention_policy": {"content_body": "allowed"},
        },
    )
    manifest = manifest_service.build_evidence_manifest(
        run_id=store.run.run_id,
        question_set_version=store.run.question_set_version,
        registry_id=store.run.registry_id,
        registry_version=store.run.registry_version,
        as_of=store.now + timedelta(minutes=1),
        snapshot_ids=(frozen.snapshot.snapshot_id,),
        derived_artifact_ids=(artifact.derived_artifact_id,),
        audit_proof_ids=(lineage["proof"].proof_id,),
        coverage_summary={"complete": 1},
    )
    assert store.repository.get_evidence_manifest(manifest.manifest_id) == manifest
    assert store.repository.list_evidence_manifests(run_id=store.run.run_id) == [
        manifest
    ]
    assert manifest_service.validate_evidence_manifest(manifest) == manifest
    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT item_type, item_id, disposition FROM evidence_manifest_items "
            "WHERE manifest_id=? ORDER BY ordinal",
            (manifest.manifest_id,),
        ).fetchall()
    assert rows == [
        ("snapshot", frozen.snapshot.snapshot_id, "included"),
        ("proof", lineage["proof"].proof_id, "excluded"),
    ]


def test_deferred_snapshot_bundle_failure_rolls_back_all_sqlite_metadata(
    acquisition_store,
):
    store = acquisition_store
    lineage = _prepare_lineage(store)
    invalid_request = _content_request(
        store, lineage, observation_id="invalid-creating-observation"
    )
    invalid_request = replace(
        invalid_request,
        attempt_id=lineage["discovery_attempt"].attempt_id,
    )
    with pytest.raises(SnapshotCommitError):
        lineage["service"].freeze_content(
            b"orphan-after-rollback",
            invalid_request,
            owner_token=lineage["token"],
            lease_epoch=1,
        )
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM raw_resource_snapshots"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM resource_observations"
        ).fetchone()[0] == 0
