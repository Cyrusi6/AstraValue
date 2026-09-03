from __future__ import annotations

import hashlib
import sqlite3

import pytest

from analysis.acquisition.bootstrap import StorageBootstrapper
from analysis.acquisition.migrations import (
    BackupError,
    MigrationCoordinator,
    MigrationError,
    create_verified_backup,
)
from test_acquisition_migration import _create_v4, _migration_count, _version


def test_verified_v4_backup_manifest_and_source_are_unchanged(tmp_path):
    path = tmp_path / "v4.db"
    _create_v4(path)
    before = path.read_bytes()
    manifest = create_verified_backup(path, tmp_path / "data", label="v4")
    target = tmp_path / "data" / manifest.target_relative_path
    assert manifest.source_schema_version == 4
    assert manifest.integrity_check == "ok"
    assert manifest.foreign_key_violations == 0
    assert hashlib.sha256(target.read_bytes()).hexdigest() == manifest.sha256
    assert path.read_bytes() == before
    assert _version(path) == 4


def test_verified_v5_backup_is_a_recovery_point(tmp_path):
    path = tmp_path / "v5.db"
    _create_v4(path)
    MigrationCoordinator(path).apply_0005()
    manifest = create_verified_backup(path, tmp_path / "data", label="v5")
    assert manifest.source_schema_version == 5
    assert _version(tmp_path / "data" / manifest.target_relative_path) == 5


def test_fresh_no_legacy_backup(tmp_path):
    result = StorageBootstrapper(
        tmp_path / "fresh.db", tmp_path / "data"
    ).bootstrap()
    assert result.backups == ()
    assert list((tmp_path / "data").glob("backups/*.db")) == []


def test_rejects_bad_integrity_without_creating_backup(tmp_path):
    path = tmp_path / "bad.db"
    path.write_bytes(b"not sqlite")
    with pytest.raises(MigrationError):
        create_verified_backup(path, tmp_path / "data")
    assert not (tmp_path / "data" / "backups").exists()


def test_backup_does_not_create_intent_or_migrate_namespace_or_run(tmp_path):
    path = tmp_path / "v5.db"
    _create_v4(path)
    MigrationCoordinator(path).apply_0005()
    before_version = _version(path)
    before_migrations = _migration_count(path, "0005_dimensional_verification_status")
    bootstrapper = StorageBootstrapper(path, tmp_path / "backup-root")
    manifest = bootstrapper.backup(label="manual")
    assert manifest.source_schema_version == 5
    assert _version(path) == before_version
    assert _migration_count(path, "0005_dimensional_verification_status") == before_migrations
    assert not bootstrapper.intent_path.exists()
    assert not bootstrapper.marker_path.exists()
    connection = sqlite3.connect(path)
    try:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "storage_namespaces" not in names
        assert "acquisition_runs" not in names
    finally:
        connection.close()
