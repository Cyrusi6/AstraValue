from __future__ import annotations

from datetime import datetime, timezone

from analysis.acquisition.models import SnapshotIntegrityStatus
from analysis.acquisition.snapshots import (
    ContentAddressedBlobStore,
    ContentSnapshotRequest,
    SnapshotIntegrityService,
    SnapshotService,
)


class Repository:
    def __init__(self):
        self.blobs = {}
        self.snapshots = {}
        self.observations = []
        self.events = []

    def find_raw_resource_snapshot(self, **filters):
        return next(
            (
                item
                for item in self.snapshots.values()
                if all(getattr(item, key) == value for key, value in filters.items())
            ),
            None,
        )

    def commit_snapshot_bundle(self, blob, snapshot, observation, **kwargs):
        self.blobs[blob.content_blob_id] = blob
        self.snapshots[snapshot.snapshot_id] = snapshot
        self.observations.append(observation)

    def get_raw_resource_snapshot(self, snapshot_id):
        return self.snapshots[snapshot_id]

    def get_content_blob(self, blob_id):
        return self.blobs[blob_id]

    def append_snapshot_integrity_event(self, event):
        self.events.append(event)

    def list_snapshot_integrity_events(self, snapshot_id):
        return [event for event in self.events if event.snapshot_id == snapshot_id]


def test_integrity_events_are_append_only_and_tamper_creates_reconcile_candidate(tmp_path):
    repository = Repository()
    store = ContentAddressedBlobStore(tmp_path / "data", "namespace-test")
    snapshots = SnapshotService(store, repository)
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    frozen = snapshots.freeze_content(
        b"%PDF-original",
        ContentSnapshotRequest(
            attempt_id="fetch-1",
            discovered_resource_id="resource-1",
            parent_discovery_attempt_id="discovery-1",
            source_definition_id="cninfo.disclosures",
            source_definition_version="1.0.0",
            canonical_resource_id="announcement-1",
            upstream_material_id="announcement-1",
            canonical_url="https://example.test/a.pdf",
            mime_type="application/pdf",
            observed_at=now,
            retrieved_at=now,
        ),
    )
    candidates = []
    integrity = SnapshotIntegrityService(
        store,
        repository,
        reconcile_candidate_sink=lambda snapshot, event: candidates.append(
            (snapshot.snapshot_id, event.integrity_event_id)
        ),
    )
    verified = integrity.verify_snapshot(frozen.snapshot.snapshot_id)
    store.resolve_blob(frozen.snapshot.archive_relative_path).write_bytes(b"tampered")
    quarantined = integrity.verify_snapshot(frozen.snapshot.snapshot_id)

    assert verified.event.status == SnapshotIntegrityStatus.VERIFIED
    assert quarantined.event.status == SnapshotIntegrityStatus.QUARANTINED
    assert [item.status for item in repository.events] == [
        SnapshotIntegrityStatus.VERIFIED,
        SnapshotIntegrityStatus.QUARANTINED,
    ]
    assert candidates == [
        (frozen.snapshot.snapshot_id, quarantined.event.integrity_event_id)
    ]
    assert integrity.current_status(frozen.snapshot.snapshot_id) == SnapshotIntegrityStatus.QUARANTINED
