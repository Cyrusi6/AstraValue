from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from analysis.acquisition.snapshots import (
    ARCHIVE_WRITE_FAILED,
    INTEGRITY_MISMATCH,
    SNAPSHOT_COMMIT_FAILED,
    ArchiveWriteError,
    ContentAddressedBlobStore,
    ContentSnapshotRequest,
    DiscoverySnapshotRequest,
    SnapshotCommitCoordinator,
    SnapshotCommitError,
    SnapshotIntegrityMismatch,
    SnapshotIntegrityService,
    SnapshotPathEscape,
    SnapshotService,
    decide_available_at,
    is_point_in_time_eligible,
)
from analysis.acquisition.models import ResourceRole, SnapshotIntegrityStatus


def _store(tmp_path: Path) -> ContentAddressedBlobStore:
    return ContentAddressedBlobStore(tmp_path / "data", "namespace-test")


class _SnapshotRepository:
    def __init__(self) -> None:
        self.blobs = {}
        self.snapshots = {}
        self.resource_observations = []
        self.discovery_observations = []
        self.integrity_events = []

    def commit_snapshot_bundle(self, blob, snapshot, observation, **kwargs):
        self.blobs.setdefault(blob.content_blob_id, blob)
        self.snapshots[snapshot.snapshot_id] = snapshot
        self.resource_observations.append(observation)

    def commit_discovery_snapshot_bundle(self, blob, snapshot, observation, **kwargs):
        self.blobs.setdefault(blob.content_blob_id, blob)
        self.snapshots[snapshot.snapshot_id] = snapshot
        self.discovery_observations.append(observation)

    def save_resource_observation(self, observation, **kwargs):
        self.resource_observations.append(observation)

    def save_discovery_observation(self, observation, **kwargs):
        self.discovery_observations.append(observation)

    def find_raw_resource_snapshot(self, **filters):
        matches = []
        for snapshot in self.snapshots.values():
            if all(
                getattr(snapshot, key) == value
                for key, value in filters.items()
                if value is not None
            ):
                matches.append(snapshot)
        return max(matches, key=lambda item: item.version) if matches else None

    def get_raw_resource_snapshot(self, snapshot_id):
        return self.snapshots[snapshot_id]

    def get_content_blob(self, content_blob_id):
        return self.blobs[content_blob_id]

    def append_snapshot_integrity_event(self, event):
        self.integrity_events.append(event)

    def list_snapshot_integrity_events(self, snapshot_id):
        return [item for item in self.integrity_events if item.snapshot_id == snapshot_id]


def test_atomic_archive_uses_full_hash_relative_path_and_reuses_bytes(tmp_path):
    store = _store(tmp_path)
    content = b"immutable disclosure bytes"
    digest = hashlib.sha256(content).hexdigest()

    first = store.archive_bytes(content)
    repeated = store.archive_bytes(content)

    assert first.sha256 == digest
    assert len(first.sha256) == 64
    assert first.byte_length == len(content)
    assert first.relative_path == f"raw/blobs/sha256/{digest[:2]}/{digest}"
    assert not first.reused
    assert repeated.reused
    assert repeated.archived_at == first.archived_at
    assert store.resolve_blob(first.relative_path).read_bytes() == content
    assert list(store.resolve_blob(first.relative_path).parent.glob("*.tmp")) == []


def test_hash_and_length_expectations_fail_before_publish(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(SnapshotIntegrityMismatch) as hash_error:
        store.archive_bytes(b"payload", expected_sha256="0" * 64)
    assert hash_error.value.reason_code == INTEGRITY_MISMATCH
    with pytest.raises(SnapshotIntegrityMismatch) as length_error:
        store.archive_bytes(b"payload", expected_length=999)
    assert length_error.value.reason_code == INTEGRITY_MISMATCH
    assert store.scan_orphans(()) == ()


def test_existing_hash_path_with_wrong_bytes_is_quarantined_not_consumed(tmp_path):
    store = _store(tmp_path)
    content = b"expected"
    digest = hashlib.sha256(content).hexdigest()
    target = store.resolve_blob(store.blob_relative_path(digest))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"collision-or-tamper")

    with pytest.raises(SnapshotIntegrityMismatch) as error:
        store.archive_bytes(content)

    assert error.value.reason_code == INTEGRITY_MISMATCH
    assert not target.exists()
    assert list((store.data_root / "quarantine" / "raw").glob(f"{digest}.*"))


def test_archive_write_failure_has_fixed_parse_failed_mapping(monkeypatch, tmp_path):
    store = _store(tmp_path)

    def fail_replace(source, destination):
        raise OSError("fixture write failure")

    monkeypatch.setattr("analysis.acquisition.snapshots.os.replace", fail_replace)
    with pytest.raises(ArchiveWriteError) as error:
        store.archive_bytes(b"payload")
    assert error.value.outcome == "parse_failed"
    assert error.value.reason_code == ARCHIVE_WRITE_FAILED


def test_snapshot_commit_failure_leaves_read_only_orphan_and_no_snapshot(tmp_path):
    class FailingRepository:
        calls = 0

        def commit_snapshot_bundle(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("fixture database failure")

    store = _store(tmp_path)
    repository = FailingRepository()
    coordinator = SnapshotCommitCoordinator(store, repository)
    with pytest.raises(SnapshotCommitError) as error:
        coordinator.publish_content_snapshot(
            b"payload",
            lambda archived: ("blob", "snapshot", "observation"),
        )
    assert error.value.outcome == "parse_failed"
    assert error.value.reason_code == SNAPSHOT_COMMIT_FAILED
    assert error.value.orphan_relative_path
    assert repository.calls == 1
    orphans = store.scan_orphans(())
    assert [item.relative_path for item in orphans] == [error.value.orphan_relative_path]
    # The scanner is deliberately read-only.
    assert store.resolve_blob(orphans[0].relative_path).exists()


@pytest.mark.parametrize(
    "path",
    ["../escape", "raw/blobs/sha256/../../escape", "C:/escape", "/escape"],
)
def test_path_escape_is_rejected_without_writing_outside_root(tmp_path, path):
    store = _store(tmp_path)
    with pytest.raises(SnapshotPathEscape):
        store.resolve_blob(path)
    assert not (tmp_path / "escape").exists()


def test_date_only_precision_uses_next_source_local_day_boundary():
    retrieved_at = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
    decision = decide_available_at(
        published_at=date(2026, 9, 3),
        published_at_precision="date",
        source_timezone="Asia/Shanghai",
        retrieved_at=retrieved_at,
        immutable_version_proven=True,
    )
    assert decision.available_at == datetime(2026, 9, 3, 16, tzinfo=timezone.utc)
    assert decision.available_at_basis == "source_date_next_boundary"
    assert not is_point_in_time_eligible(
        decision.available_at,
        datetime(2026, 9, 3, 2, tzinfo=timezone.utc),
    )
    assert is_point_in_time_eligible(
        decision.available_at,
        datetime(2026, 9, 3, 16, tzinfo=timezone.utc),
    )


def test_unversioned_historical_page_uses_retrieved_at_and_prevents_future_leak():
    retrieved_at = datetime(2026, 9, 4, 8, tzinfo=timezone.utc)
    decision = decide_available_at(
        published_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        published_at_precision="instant",
        source_timezone="Asia/Shanghai",
        retrieved_at=retrieved_at,
        immutable_version_proven=False,
    )
    assert decision.available_at == retrieved_at
    assert decision.available_at_basis == "retrieved_at"
    assert not is_point_in_time_eligible(
        decision.available_at,
        retrieved_at - timedelta(microseconds=1),
    )


def _content_request(**overrides):
    values = {
        "attempt_id": "attempt-fetch-1",
        "discovered_resource_id": "resource-1",
        "parent_discovery_attempt_id": "attempt-discovery-1",
        "source_definition_id": "cninfo.disclosures",
        "source_definition_version": "1.0.0",
        "canonical_resource_id": "announcement-1",
        "upstream_material_id": "announcement-1",
        "canonical_url": "https://example.test/a.pdf",
        "mime_type": "application/pdf",
        "observed_at": datetime(2026, 9, 4, 8, tzinfo=timezone.utc),
        "retrieved_at": datetime(2026, 9, 4, 8, tzinfo=timezone.utc),
        "published_at_raw": "2026-09-03",
        "published_at": date(2026, 9, 3),
        "published_at_precision": "date",
        "source_timezone": "Asia/Shanghai",
        "immutable_version_proven": True,
        "request_summary": {"Authorization": "secret"},
        "response_summary": {"Content-Type": "application/pdf"},
        "etag": '"v1"',
    }
    values.update(overrides)
    return ContentSnapshotRequest(**values)


def test_same_url_mixed_disposition_new_hash_versions_and_unchanged_observation(tmp_path):
    repository = _SnapshotRepository()
    service = SnapshotService(_store(tmp_path), repository)
    first = service.freeze_content(b"%PDF-v1", _content_request())
    unchanged = service.freeze_content(
        b"%PDF-v1",
        _content_request(attempt_id="attempt-fetch-2", etag='"v2"'),
    )
    changed = service.freeze_content(
        b"%PDF-v2",
        _content_request(attempt_id="attempt-fetch-3", etag='"v2"'),
    )

    assert first.disposition == "new"
    assert unchanged.disposition == "unchanged"
    assert unchanged.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert not unchanged.created_snapshot
    assert changed.disposition == "changed"
    assert changed.snapshot.version == 2
    assert changed.snapshot.supersedes_snapshot_id == first.snapshot.snapshot_id
    # Replacement bytes retain the disclosed date without acquiring the old
    # version's historical point-in-time eligibility.
    assert changed.snapshot.published_at == first.snapshot.published_at
    assert changed.snapshot.published_at_precision == "date"
    assert changed.snapshot.available_at == _content_request().retrieved_at
    assert changed.snapshot.available_at_basis == "retrieved_at"
    assert not is_point_in_time_eligible(
        changed.snapshot.available_at, first.snapshot.available_at,
    )
    assert len(repository.snapshots) == 2
    assert len(repository.resource_observations) == 3
    assert repository.resource_observations[1].etag == '"v2"'
    assert repository.resource_observations[0].request_summary["Authorization"] == "[REDACTED]"


def test_mirror_provenance_uses_shared_blob_but_distinct_snapshots(tmp_path):
    repository = _SnapshotRepository()
    service = SnapshotService(_store(tmp_path), repository)
    cninfo = service.freeze_content(b"%PDF-shared", _content_request())
    sse = service.freeze_content(
        b"%PDF-shared",
        _content_request(
            source_definition_id="sse.disclosures",
            canonical_url="https://example.test/sse.pdf",
            attempt_id="attempt-sse",
        ),
    )
    assert cninfo.blob.content_blob_id == sse.blob.content_blob_id
    assert cninfo.snapshot.snapshot_id != sse.snapshot.snapshot_id
    assert cninfo.snapshot.upstream_material_id == sse.snapshot.upstream_material_id
    assert len(repository.blobs) == 1


def test_discovery_response_has_query_page_identity_and_no_document_fields(tmp_path):
    repository = _SnapshotRepository()
    service = SnapshotService(_store(tmp_path), repository)
    result = service.freeze_discovery_response(
        b'{"rows": []}',
        DiscoverySnapshotRequest(
            attempt_id="attempt-discovery-1",
            physical_query_plan_item_id="plan-1",
            source_definition_id="cninfo.disclosures",
            source_definition_version="1.0.0",
            query_page_canonical="plan-1:page:1",
            mime_type="application/json",
            observed_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
            page_number=1,
        ),
    )
    assert result.snapshot.resource_role == ResourceRole.DISCOVERY_RESPONSE
    assert result.snapshot.query_page_canonical == "plan-1:page:1"
    assert result.snapshot.canonical_resource_id is None
    assert result.snapshot.upstream_material_id is None
    assert result.snapshot.published_at is None


def test_snapshot_role_available_at_same_day_cutoff_and_observation_metadata(tmp_path):
    repository = _SnapshotRepository()
    service = SnapshotService(_store(tmp_path), repository)
    result = service.freeze_content(
        b"%PDF-point-in-time",
        _content_request(
            original_url="https://user:password@example.test/a.pdf?token=secret&safe=1",
            final_url="https://example.test/final.pdf?api_key=secret",
            redirect_chain=("https://example.test/hop?session=secret",),
            last_modified="Wed, 03 Sep 2026 08:00:00 GMT",
        ),
    )

    assert result.snapshot.resource_role == ResourceRole.CONTENT
    assert result.snapshot.available_at == datetime(2026, 9, 3, 16, tzinfo=timezone.utc)
    assert result.observation.original_url == (
        "https://example.test/a.pdf?token=%5BREDACTED%5D&safe=1"
    )
    assert result.observation.final_url == (
        "https://example.test/final.pdf?api_key=%5BREDACTED%5D"
    )
    assert result.observation.redirect_chain == (
        {"url": "https://example.test/hop?session=%5BREDACTED%5D"},
    )
    assert result.observation.last_modified == "Wed, 03 Sep 2026 08:00:00 GMT"
    assert "etag" not in type(result.snapshot).model_fields


def test_integrity_tamper_appends_quarantine_event_without_mutating_snapshot(tmp_path):
    repository = _SnapshotRepository()
    store = _store(tmp_path)
    service = SnapshotService(store, repository)
    frozen = service.freeze_content(b"%PDF-original", _content_request())
    before = frozen.snapshot.model_dump_json()
    store.resolve_blob(frozen.snapshot.archive_relative_path).write_bytes(b"tampered")
    candidates = []
    integrity = SnapshotIntegrityService(
        store,
        repository,
        reconcile_candidate_sink=lambda snapshot, event: candidates.append(snapshot.snapshot_id)
        or {"snapshot_id": snapshot.snapshot_id},
    )
    result = integrity.verify_snapshot(frozen.snapshot.snapshot_id)

    assert result.event.status == SnapshotIntegrityStatus.QUARANTINED
    assert result.event.reason_code in {"blob_length_mismatch", "blob_hash_mismatch"}
    assert frozen.snapshot.model_dump_json() == before
    assert len(repository.integrity_events) == 1
    assert candidates == [frozen.snapshot.snapshot_id]
    assert integrity.current_status(frozen.snapshot.snapshot_id) == SnapshotIntegrityStatus.QUARANTINED
