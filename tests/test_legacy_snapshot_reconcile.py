from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from analysis.acquisition.legacy import reconcile_legacy_document_snapshot
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.models import DocumentRecord, SourceRecord, SyncResult
from analysis.storage import ReportStorage


NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


def _runtime(tmp_path):
    return AcquisitionRuntime.create(tmp_path / "analysis.db", tmp_path / "data")


def _document(path, sha256):
    source = SourceRecord(
        source_id="legacy-source",
        name="legacy official archive",
        url="https://www.cninfo.com.cn/legacy/example.pdf",
        published_at=NOW - timedelta(days=3650),
        retrieved_at=NOW,
        document_hash=sha256,
        upstream_source_id="legacy-upstream:example",
    )
    return DocumentRecord(
        document_id="legacy-document-1",
        ticker="600519",
        title="历史公告",
        archived_path=str(path),
        text_path=str(path) + ".txt",
        sha256=sha256,
        source=source,
    )


def _count(runtime, table):
    with sqlite3.connect(runtime.db_path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_matching_legacy_raw_creates_verified_snapshot_without_attempts(tmp_path):
    raw = tmp_path / "legacy.pdf"
    raw.write_bytes(b"frozen legacy pdf bytes")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    runtime = _runtime(tmp_path)
    document = _document(raw, digest)
    document_before = document.model_dump_json()
    misleading_legacy_sync = SyncResult(
        sync_result_id="legacy-provider-text-must-not-drive-reconcile",
        ticker="600519",
        provider_results={
            "official": "success/no data/checkpoint advanced (untrusted legacy text)"
        },
        as_of=NOW,
        created_at=NOW,
    )
    storage = ReportStorage(runtime.db_path)
    storage.save_sync_result(misleading_legacy_sync)
    with sqlite3.connect(runtime.db_path) as connection:
        sync_payload_before = connection.execute(
            "SELECT payload FROM sync_results WHERE sync_result_id=?",
            (misleading_legacy_sync.sync_result_id,),
        ).fetchone()[0]

    result = reconcile_legacy_document_snapshot(runtime, document)

    assert result.status == "linked"
    assert result.reason_code == "legacy_raw_hash_verified"
    snapshot = runtime.repository.get_raw_resource_snapshot(result.snapshot_id)
    assert snapshot.sha256 == digest
    assert snapshot.available_at == NOW
    assert snapshot.available_at > document.source.published_at
    assert snapshot.available_at_basis.value == "legacy_verified"
    assert "no_attempt_or_checkpoint_inferred" in snapshot.policy_decision
    assert runtime.repository.list_attempts() == []
    assert _count(runtime, "source_checkpoints") == 0
    assert _count(runtime, "resource_observations") == 0
    assert document.raw_resource_snapshot_id is None
    assert document.model_dump_json() == document_before
    with sqlite3.connect(runtime.db_path) as connection:
        assert connection.execute(
            "SELECT payload FROM sync_results WHERE sync_result_id=?",
            (misleading_legacy_sync.sync_result_id,),
        ).fetchone()[0] == sync_payload_before

    repeated = reconcile_legacy_document_snapshot(runtime, document)
    assert repeated.status == "linked"
    assert repeated.reason_code == "legacy_raw_already_verified"
    assert repeated.snapshot_id == result.snapshot_id
    assert _count(runtime, "raw_resource_snapshots") == 1


def test_missing_legacy_raw_remains_unresolved_and_writes_nothing(tmp_path):
    runtime = _runtime(tmp_path)
    missing = tmp_path / "missing.pdf"
    result = reconcile_legacy_document_snapshot(
        runtime,
        _document(missing, "0" * 64),
    )
    assert result.status == "unresolved"
    assert result.reason_code == "legacy_raw_missing"
    assert _count(runtime, "raw_resource_snapshots") == 0
    assert _count(runtime, "content_blobs") == 0


def test_tampered_legacy_raw_never_creates_snapshot(tmp_path):
    raw = tmp_path / "tampered.pdf"
    original = b"original legacy bytes"
    raw.write_bytes(original)
    document = _document(raw, hashlib.sha256(original).hexdigest())
    raw.write_bytes(b"modified after old document record")
    runtime = _runtime(tmp_path)

    result = reconcile_legacy_document_snapshot(runtime, document)

    assert result.status == "unresolved"
    assert result.reason_code == "legacy_raw_hash_mismatch"
    assert _count(runtime, "raw_resource_snapshots") == 0
    assert _count(runtime, "content_blobs") == 0
