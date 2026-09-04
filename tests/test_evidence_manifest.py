from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analysis.acquisition.manifests import EvidenceGateError, EvidenceManifestService
from analysis.acquisition.models import SnapshotIntegrityEvent, SnapshotIntegrityStatus
from analysis.acquisition.registry import INITIAL_REGISTRY_PATH, SourceRegistryLoader
from analysis.acquisition.snapshots import (
    ContentAddressedBlobStore,
    ContentSnapshotRequest,
    DiscoverySnapshotRequest,
    SnapshotService,
)


NOW = datetime(2026, 9, 4, 8, tzinfo=timezone.utc)


class Repository:
    def __init__(self):
        self.blobs = {}
        self.snapshots = {}
        self.resource_observations = []
        self.discovery_observations = []
        self.integrity_events = []
        self.artifacts = {}
        self.manifests = {}

    def commit_snapshot_bundle(self, blob, snapshot, observation, **kwargs):
        self.blobs.setdefault(blob.content_blob_id, blob)
        self.snapshots[snapshot.snapshot_id] = snapshot
        self.resource_observations.append(observation)

    def commit_discovery_snapshot_bundle(self, blob, snapshot, observation, **kwargs):
        self.blobs.setdefault(blob.content_blob_id, blob)
        self.snapshots[snapshot.snapshot_id] = snapshot
        self.discovery_observations.append(observation)

    def find_raw_resource_snapshot(self, **filters):
        values = [
            item
            for item in self.snapshots.values()
            if all(getattr(item, key) == value for key, value in filters.items())
        ]
        return max(values, key=lambda item: item.version) if values else None

    def get_raw_resource_snapshot(self, snapshot_id):
        return self.snapshots[snapshot_id]

    def list_snapshot_integrity_events(self, snapshot_id):
        return [item for item in self.integrity_events if item.snapshot_id == snapshot_id]

    def get_derived_artifact(self, artifact_id):
        return self.artifacts[artifact_id]

    def save_evidence_manifest(self, manifest, items=()):
        previous = self.manifests.get(manifest.manifest_id)
        if previous is not None and previous != manifest:
            raise ValueError("manifest immutable")
        self.manifests[manifest.manifest_id] = manifest


def _request(**overrides):
    values = dict(
        attempt_id="fetch-1",
        discovered_resource_id="resource-1",
        parent_discovery_attempt_id="discovery-1",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="announcement-1",
        upstream_material_id="announcement-1",
        canonical_url="https://example.test/a.pdf",
        mime_type="application/pdf",
        observed_at=NOW,
        retrieved_at=NOW,
        published_at=NOW - timedelta(days=1),
        published_at_precision="instant",
        immutable_version_proven=True,
    )
    values.update(overrides)
    return ContentSnapshotRequest(**values)


def _policy(llm="allowed", *, live_review="approved"):
    return {
        "policy_status": "enabled",
        "enabled": True,
        "access_method": "https_api",
        "live_access_review": {"status": live_review},
        "license_policy": {"llm_processing": llm, "archive_original": "allowed"},
        "retention_policy": {"content_body": "allowed"},
    }


def _services(tmp_path, policy="allowed", *, live_review="approved"):
    repository = Repository()
    store = ContentAddressedBlobStore(tmp_path / "data", "namespace-test")
    snapshots = SnapshotService(store, repository)
    manifests = EvidenceManifestService(
        store,
        repository,
        lambda source_id, version: _policy(policy, live_review=live_review),
    )
    return repository, store, snapshots, manifests


def _build(manifests, snapshot_ids, **kwargs):
    return manifests.build_evidence_manifest(
        run_id="run-1",
        question_set_version="business-model-v1",
        registry_id="business-model-sources",
        registry_version="1.0.0",
        as_of=NOW,
        snapshot_ids=snapshot_ids,
        coverage_summary={"coverage_accounted": True},
        **kwargs,
    )


def test_eligible_manifest_is_deterministic_and_resolves_only_after_validation(tmp_path):
    repository, _, snapshots, manifests = _services(tmp_path)
    frozen = snapshots.freeze_content(b"%PDF-content", _request())
    first = _build(manifests, [frozen.snapshot.snapshot_id])
    repeated = _build(manifests, [frozen.snapshot.snapshot_id])

    assert first.model_dump_json() == repeated.model_dump_json()
    assert first.gate_passed
    assert len(first.manifest_hash) == 64
    assert manifests.resolve_llm_materials(first) == (b"%PDF-content",)
    assert list(repository.manifests) == [first.manifest_id]


def test_discovery_response_is_audit_only_and_never_automatic_llm_content(tmp_path):
    _, _, snapshots, manifests = _services(tmp_path)
    content = snapshots.freeze_content(b"%PDF-content", _request())
    discovery = snapshots.freeze_discovery_response(
        b'{"rows":[]}',
        DiscoverySnapshotRequest(
            attempt_id="discovery-1",
            physical_query_plan_item_id="plan-1",
            source_definition_id="cninfo.disclosures",
            source_definition_version="1.0.0",
            query_page_canonical="plan-1:page:1",
            mime_type="application/json",
            observed_at=NOW,
            retrieved_at=NOW,
            page_number=1,
        ),
    )
    manifest = _build(
        manifests,
        [content.snapshot.snapshot_id, discovery.snapshot.snapshot_id],
    )
    assert [item.snapshot_id for item in manifest.items] == [content.snapshot.snapshot_id]
    assert any(
        item.object_id == discovery.snapshot.snapshot_id
        and item.reason_code == "discovery_response_audit_only"
        for item in manifest.exclusions
    )


@pytest.mark.parametrize("failure", ["missing", "future", "denied", "tampered"])
def test_manifest_fails_closed_for_ineligible_content(tmp_path, failure):
    policy = "denied" if failure == "denied" else "allowed"
    repository, store, snapshots, manifests = _services(tmp_path, policy)
    request = _request(
        observed_at=NOW + timedelta(days=1),
        retrieved_at=NOW + timedelta(days=1),
        published_at=NOW + timedelta(days=1),
    ) if failure == "future" else _request()
    frozen = snapshots.freeze_content(b"%PDF-content", request)
    snapshot_id = "missing" if failure == "missing" else frozen.snapshot.snapshot_id
    if failure == "tampered":
        store.resolve_blob(frozen.snapshot.archive_relative_path).write_bytes(b"bad")
    with pytest.raises(EvidenceGateError) as error:
        _build(manifests, [snapshot_id])
    assert error.value.reason_code == "evidence_gate_failed"


def test_quarantined_snapshot_cannot_enter_new_manifest(tmp_path):
    repository, _, snapshots, manifests = _services(tmp_path)
    frozen = snapshots.freeze_content(b"%PDF-content", _request())
    repository.integrity_events.append(
        SnapshotIntegrityEvent(
            snapshot_id=frozen.snapshot.snapshot_id,
            status=SnapshotIntegrityStatus.QUARANTINED,
            reason_code="fixture_tamper",
        )
    )
    with pytest.raises(EvidenceGateError) as error:
        _build(manifests, [frozen.snapshot.snapshot_id])
    assert error.value.exclusions[0].reason_code == "snapshot_quarantined"


def test_historical_pending_live_review_snapshot_cannot_enter_new_manifest(tmp_path):
    repository = Repository()
    store = ContentAddressedBlobStore(tmp_path / "data", "namespace-test")
    snapshots = SnapshotService(store, repository)
    registry = SourceRegistryLoader().load_registry(INITIAL_REGISTRY_PATH)
    manifests = EvidenceManifestService(
        store,
        repository,
        lambda source_id, version: registry.definition(source_id),
    )
    frozen = snapshots.freeze_content(b"%PDF-content", _request())

    with pytest.raises(EvidenceGateError) as error:
        _build(manifests, [frozen.snapshot.snapshot_id])

    assert error.value.exclusions[0].reason_code == (
        "source_live_access_not_approved"
    )
    assert repository.manifests == {}


def test_existing_manifest_revalidates_live_review_authority(tmp_path):
    repository = Repository()
    store = ContentAddressedBlobStore(tmp_path / "data", "namespace-test")
    snapshots = SnapshotService(store, repository)
    policy = _policy()
    manifests = EvidenceManifestService(
        store,
        repository,
        lambda source_id, version: policy,
    )
    frozen = snapshots.freeze_content(b"%PDF-content", _request())
    manifest = _build(manifests, [frozen.snapshot.snapshot_id])

    policy["live_access_review"]["status"] = "pending"
    with pytest.raises(EvidenceGateError) as error:
        manifests.validate_evidence_manifest(manifest)

    assert error.value.reason_code == "source_live_access_not_approved"
