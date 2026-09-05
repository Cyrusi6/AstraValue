from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import traceback
import uuid
from pathlib import Path

import pytest

from analysis.acquisition.bootstrap import (
    BINDING_INTENT_SUFFIX,
    ROOT_MARKER_NAME,
    InterProcessFileLock,
    StorageBootstrapper,
    StorageNamespaceMismatch,
    StorageRepairRequired,
)
from test_acquisition_migration import _create_v4, _migration_count, _version


def _bootstrap_worker(db: str, root: str, queue) -> None:
    try:
        result = StorageBootstrapper(
            Path(db), Path(root), lock_timeout_seconds=10
        ).bootstrap()
        queue.put(("ok", result.namespace.namespace_id, result.created))
    except Exception as exc:  # pragma: no cover - asserted in parent process
        queue.put(("error", type(exc).__name__, str(exc), traceback.format_exc()))


def _empty_file_lock_worker(identity: str, queue) -> None:
    try:
        with InterProcessFileLock(identity, timeout_seconds=0.1):
            queue.put(("acquired",))
    except Exception as exc:  # pragma: no cover - asserted in parent process
        queue.put((type(exc).__name__, traceback.format_exc()))


def test_empty_file_locked_by_another_process_times_out_then_reopens():
    identity = f"empty-file-regression-{uuid.uuid4().hex}"
    lock = InterProcessFileLock(identity)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_empty_file_lock_worker, args=(identity, queue))
    try:
        with lock.path.open("x+b") as holder:
            # Both operating systems allow locking a range beyond EOF. The
            # contender must wait for the lock before attempting any writes.
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            process.start()
            result = queue.get(timeout=20)
            process.join(timeout=20)
            assert process.exitcode == 0
            assert result[0] == "BootstrapLockTimeout", result
        with InterProcessFileLock(identity):
            pass
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=20)
        queue.close()
        lock.path.unlink()


def test_create_and_reopen_preserve_one_namespace(tmp_path):
    db = tmp_path / "analysis.db"
    root = tmp_path / "data"
    first = StorageBootstrapper(db, root).bootstrap()
    second = StorageBootstrapper(db, root).bootstrap()
    assert first.created is True
    assert second.created is False
    assert first.namespace == second.namespace
    marker = json.loads((root / ROOT_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["state"] == "bound"


def test_mismatch_and_relative_path_metadata_fail_closed(tmp_path):
    db = tmp_path / "analysis.db"
    first_root = tmp_path / "first"
    StorageBootstrapper(db, first_root).bootstrap()
    other = tmp_path / "other"
    with pytest.raises(StorageNamespaceMismatch):
        StorageBootstrapper(db, other).bootstrap()
    assert not other.exists()
    intent = json.loads(
        Path(str(db) + BINDING_INTENT_SUFFIX).read_text(encoding="utf-8")
    )
    serialized = json.dumps(intent)
    assert str(tmp_path) not in serialized
    assert ":\\" not in serialized and ":/" not in serialized


def test_crash_after_db_intent_before_root_marker_only_intended_root_can_resume(tmp_path):
    db = tmp_path / "analysis.db"
    intended = tmp_path / "intended"
    with pytest.raises(RuntimeError, match="after_db_intent"):
        StorageBootstrapper(
            db,
            intended,
            fault_at="crash_after_db_intent_before_root_marker",
        ).bootstrap()
    assert not intended.exists()
    other = tmp_path / "other"
    with pytest.raises(StorageNamespaceMismatch):
        StorageBootstrapper(db, other).bootstrap()
    assert not other.exists()
    assert StorageBootstrapper(db, intended).bootstrap().namespace.namespace_id


def test_same_db_other_root_rejected_before_other_root_write(tmp_path):
    db = tmp_path / "analysis.db"
    StorageBootstrapper(db, tmp_path / "a").bootstrap()
    other = tmp_path / "b"
    with pytest.raises(StorageNamespaceMismatch):
        StorageBootstrapper(db, other).bootstrap()
    assert not other.exists()


def test_same_root_other_db_rejected_before_other_database_write(tmp_path):
    root = tmp_path / "data"
    first = tmp_path / "one.db"
    StorageBootstrapper(first, root).bootstrap()
    second = tmp_path / "two.db"
    with pytest.raises(StorageNamespaceMismatch):
        StorageBootstrapper(second, root).bootstrap()
    assert not second.exists()
    assert not Path(str(second) + BINDING_INTENT_SUFFIX).exists()


def test_intended_root_resume_keeps_nonce(tmp_path):
    db = tmp_path / "analysis.db"
    root = tmp_path / "data"
    with pytest.raises(RuntimeError):
        StorageBootstrapper(db, root, fault_at="after_root_pending").bootstrap()
    before = json.loads(
        Path(str(db) + BINDING_INTENT_SUFFIX).read_text(encoding="utf-8")
    )
    result = StorageBootstrapper(db, root).bootstrap()
    assert result.namespace.binding_nonce == before["binding_nonce"]


def test_crash_after_0005_before_v5_backup_does_not_repeat_0005(tmp_path):
    db = tmp_path / "legacy.db"
    _create_v4(db)
    root = tmp_path / "data"
    with pytest.raises(RuntimeError, match="after_0005"):
        StorageBootstrapper(
            db, root, fault_at="crash_after_0005_before_v5_backup"
        ).bootstrap()
    assert _version(db) == 5
    assert _migration_count(db, "0005_dimensional_verification_status") == 1
    StorageBootstrapper(db, root).bootstrap()
    assert _version(db) == 6
    assert _migration_count(db, "0005_dimensional_verification_status") == 1


def test_crash_after_v5_backup_before_0006_resumes_without_skip(tmp_path):
    db = tmp_path / "legacy.db"
    _create_v4(db)
    root = tmp_path / "data"
    with pytest.raises(RuntimeError, match="after_v5_backup"):
        StorageBootstrapper(
            db, root, fault_at="crash_after_v5_backup_before_0006"
        ).bootstrap()
    assert _version(db) == 5
    assert list(root.glob("backups/*recovery-v5*.db"))
    StorageBootstrapper(db, root).bootstrap()
    assert _version(db) == 6


def test_crash_after_db_namespace_commit_matching_pending_resume(tmp_path):
    db = tmp_path / "analysis.db"
    root = tmp_path / "data"
    with pytest.raises(RuntimeError, match="after_db_namespace_commit"):
        StorageBootstrapper(
            db, root, fault_at="crash_after_db_namespace_commit"
        ).bootstrap()
    marker = json.loads((root / ROOT_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["state"] == "pending"
    result = StorageBootstrapper(db, root).bootstrap()
    assert result.created is False
    marker = json.loads((root / ROOT_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["state"] == "bound"


def test_missing_marker_fail_closed(tmp_path):
    db = tmp_path / "analysis.db"
    root = tmp_path / "data"
    StorageBootstrapper(db, root).bootstrap()
    (root / ROOT_MARKER_NAME).unlink()
    with pytest.raises(StorageRepairRequired, match="marker is missing"):
        StorageBootstrapper(db, root).bootstrap()


def test_nonce_or_fingerprint_conflict_requires_repair(tmp_path):
    db = tmp_path / "analysis.db"
    root = tmp_path / "data"
    with pytest.raises(RuntimeError):
        StorageBootstrapper(db, root, fault_at="after_root_pending").bootstrap()
    marker_path = root / ROOT_MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["binding_nonce"] = "conflicting-nonce"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(StorageRepairRequired, match="nonce"):
        StorageBootstrapper(db, root).bootstrap()


def test_concurrent_bootstrap_single_winner_with_real_processes(tmp_path):
    db = tmp_path / "analysis.db"
    root = tmp_path / "data"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_bootstrap_worker, args=(str(db), str(root), queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert all(item[0] == "ok" for item in results), results
    assert len({item[1] for item in results}) == 1
    assert sorted(item[2] for item in results) == [False, True]
