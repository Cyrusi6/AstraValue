from __future__ import annotations

import hashlib
import sqlite3

import pytest

from analysis.acquisition.bootstrap import StorageBootstrapper
from analysis.acquisition.migrations import (
    BackupError,
    MigrationCoordinator,
    MigrationError,
    open_legacy_v5_readonly,
    restore_verified_backup,
)
from analysis.acquisition.runtime import acquisition_v1_enabled
from analysis.models import SyncResult
from analysis.storage import ReportStorage
from test_acquisition_migration import _create_v4, _migration_count, _version


def test_feature_flag_disable_enable_keeps_legacy_reads_and_v6_records(
    acquisition_store, monkeypatch
):
    store = acquisition_store
    monkeypatch.delenv("ASTRAVALUE_ACQUISITION_V1_ENABLED", raising=False)
    assert acquisition_v1_enabled() is False
    assert acquisition_v1_enabled(False) is False

    legacy_storage = ReportStorage(store.db_path, migration_data_root=store.data_root)
    legacy_result = SyncResult(
        sync_result_id="rollback-legacy-sync",
        ticker="600519",
        provider_results={"official": "legacy remains readable"},
    )
    legacy_storage.save_sync_result(legacy_result)
    assert legacy_storage.get_sync_result(legacy_result.sync_result_id) == legacy_result
    run_before = store.repository.get_run(store.run.run_id)

    monkeypatch.setenv("ASTRAVALUE_ACQUISITION_V1_ENABLED", "true")
    assert acquisition_v1_enabled() is True
    assert store.repository.get_run(store.run.run_id) == run_before
    assert store.repository.latest_checkpoint(
        store.run.ticker,
        store.plan_item.source_definition_id,
        store.plan_item.source_definition_version,
        store.run.question_set_version,
    ) is None


def test_v4_to_v5_recovery_point_restores_to_separate_readonly_path(tmp_path):
    working = tmp_path / "working.db"
    legacy_payload = _create_v4(working)
    result = StorageBootstrapper(working, tmp_path / "bound-data").bootstrap()
    assert _version(working) == 6
    assert [item.source_schema_version for item in result.backups] == [4, 5]
    v5_manifest = result.backups[1]
    working_hash_before = hashlib.sha256(working.read_bytes()).hexdigest()

    restored = tmp_path / "legacy-reader" / "restored-v5.db"
    restored_preflight = restore_verified_backup(
        v5_manifest, tmp_path / "bound-data", restored
    )
    assert restored_preflight.version == 5
    assert _version(restored) == 5
    assert hashlib.sha256(working.read_bytes()).hexdigest() == working_hash_before
    reader = open_legacy_v5_readonly(restored)
    try:
        assert reader.execute(
            "SELECT payload FROM dimensional_facts WHERE dimensional_fact_id='legacy'"
        ).fetchone()[0] == legacy_payload
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            reader.execute("CREATE TABLE forbidden(value TEXT)")
    finally:
        reader.close()
    assert _version(working) == 6
    assert _migration_count(working, "0006_business_model_acquisition_v1") == 1
    with pytest.raises(BackupError, match="new path"):
        restore_verified_backup(v5_manifest, tmp_path / "bound-data", restored)


def test_fault_in_0006_keeps_verified_v5_and_old_payload(tmp_path):
    database = tmp_path / "fault-v5.db"
    legacy_payload = _create_v4(database)
    MigrationCoordinator(database).apply_0005()

    def fail_during_0006(stage: str) -> None:
        if stage == "0006:5":
            raise RuntimeError("fault during 0006")

    with pytest.raises(RuntimeError, match="fault during 0006"):
        MigrationCoordinator(database, fault_injector=fail_during_0006).migrate(
            data_root=tmp_path / "recovery-data"
        )
    assert _version(database) == 5
    assert _migration_count(database, "0006_business_model_acquisition_v1") == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT payload FROM dimensional_facts WHERE dimensional_fact_id='legacy'"
        ).fetchone()[0] == legacy_payload


def test_old_reader_rejects_v6_without_modifying_working_database(tmp_path):
    database = tmp_path / "v6.db"
    MigrationCoordinator(database).migrate()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(MigrationError, match="refuses schema v6"):
        open_legacy_v5_readonly(database)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
