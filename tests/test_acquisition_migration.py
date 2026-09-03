from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from analysis.acquisition.bootstrap import StorageBootstrapper
from analysis.acquisition.migrations import MigrationCoordinator, MigrationError


def _create_v4(path: Path) -> str:
    payload = json.dumps(
        {"dimensional_fact_id": "legacy", "verification_status": "单一权威来源"},
        ensure_ascii=False,
        sort_keys=True,
    )
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE dimensional_facts (
            dimensional_fact_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            dimension_type TEXT NOT NULL,
            period_end TEXT,
            available_at TEXT NOT NULL,
            data_snapshot_id TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        PRAGMA user_version=4;
        """
    )
    connection.execute(
        "INSERT INTO dimensional_facts VALUES (?,?,?,?,?,?,?,?)",
        (
            "legacy",
            "600519",
            "metric",
            "product",
            None,
            "2026-01-01T00:00:00+00:00",
            "old-snapshot",
            payload,
        ),
    )
    connection.commit()
    connection.close()
    return payload


def _version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()

def _migration_count(path: Path, migration_id: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE migration_id=?",
                (migration_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_fresh_to_v6_is_one_transaction_and_records_each_migration_once(tmp_path):
    path = tmp_path / "fresh.db"
    MigrationCoordinator(path).migrate()
    assert _version(path) == 6
    assert _migration_count(path, "0005_dimensional_verification_status") == 1
    assert _migration_count(path, "0006_business_model_acquisition_v1") == 1


def test_v4_to_v6_keeps_payload_and_creates_two_verified_recovery_points(tmp_path):
    path = tmp_path / "v4.db"
    payload = _create_v4(path)
    result = StorageBootstrapper(path, tmp_path / "data").bootstrap()
    assert _version(path) == 6
    assert [item.source_schema_version for item in result.backups] == [4, 5]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT payload FROM dimensional_facts WHERE dimensional_fact_id='legacy'"
        ).fetchone()[0] == payload
    finally:
        connection.close()
    assert _migration_count(path, "0005_dimensional_verification_status") == 1
    assert _migration_count(path, "0006_business_model_acquisition_v1") == 1


def test_v5_to_v6_uses_verified_v5_recovery_point(tmp_path):
    path = tmp_path / "v5.db"
    _create_v4(path)
    MigrationCoordinator(path).apply_0005()
    result = StorageBootstrapper(path, tmp_path / "data").bootstrap()
    assert _version(path) == 6
    assert [item.source_schema_version for item in result.backups] == [5]


def test_v6_noop_does_not_rewrite_database(tmp_path):
    path = tmp_path / "v6.db"
    MigrationCoordinator(path).migrate()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert MigrationCoordinator(path).migrate(data_root=tmp_path / "data") == ()
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert after == before


def test_dirty_v0_rejected_before_ddl(tmp_path):
    path = tmp_path / "dirty.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE user_data(value TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="dirty v0"):
        MigrationCoordinator(path).migrate()
    assert path.read_bytes() == before


def test_future_version_rejected_before_dml(tmp_path):
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=7")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="future"):
        MigrationCoordinator(path).migrate()
    assert path.read_bytes() == before


def test_fault_rollback_keeps_v5_and_legacy_payload(tmp_path):
    path = tmp_path / "fault.db"
    payload = _create_v4(path)
    MigrationCoordinator(path).apply_0005()

    def fault(stage: str) -> None:
        if stage == "0006:5":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        MigrationCoordinator(path, fault_injector=fault).migrate(
            data_root=tmp_path / "data"
        )
    assert _version(path) == 5
    assert _migration_count(path, "0006_business_model_acquisition_v1") == 0
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT payload FROM dimensional_facts WHERE dimensional_fact_id='legacy'"
        ).fetchone()[0] == payload
    finally:
        connection.close()
