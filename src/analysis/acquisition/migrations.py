from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote


LATEST_SCHEMA_VERSION = 6
MIGRATION_0005 = "0005_dimensional_verification_status"
MIGRATION_0006 = "0006_business_model_acquisition_v1"
SUPPORTED_LEGACY_VERSIONS = {4, 5}


class MigrationError(RuntimeError):
    """The database cannot be migrated without violating the storage contract."""


class BackupError(MigrationError):
    """A required, verified recovery point could not be produced."""


@dataclass(frozen=True)
class DatabasePreflight:
    exists: bool
    version: int
    user_tables: tuple[str, ...]
    migration_ids: tuple[str, ...]
    integrity_check: str
    foreign_key_violations: int
    database_identity_hash: str
    database_fingerprint: str


@dataclass(frozen=True)
class BackupManifest:
    manifest_version: int
    source_schema_version: int
    integrity_check: str
    foreign_key_violations: int
    sha256: str
    byte_length: int
    source_identity_hash: str
    target_identity_hash: str
    target_relative_path: str
    created_at: str

    def canonical_json(self) -> str:
        return json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path_identity(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve(strict=False)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def inspect_database(db_path: Path | str) -> DatabasePreflight:
    """Inspect without creating a database, WAL file, migration row, or namespace."""

    path = Path(db_path)
    identity = _path_identity(path)
    if not path.exists():
        return DatabasePreflight(
            exists=False,
            version=0,
            user_tables=(),
            migration_ids=(),
            integrity_check="ok",
            foreign_key_violations=0,
            database_identity_hash=identity,
            database_fingerprint=hashlib.sha256(b"absent").hexdigest(),
        )

    sha256, _ = _file_sha256(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = _readonly_connection(path)
        try:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity = "\n".join(str(row[0]) for row in integrity_rows)
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            )
            if "schema_migrations" in tables:
                migration_ids = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
                    ).fetchall()
                )
            else:
                migration_ids = ()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"SQLite preflight failed: {path.name}") from exc

    return DatabasePreflight(
        exists=True,
        version=version,
        user_tables=tables,
        migration_ids=migration_ids,
        integrity_check=integrity,
        foreign_key_violations=foreign_keys,
        database_identity_hash=identity,
        database_fingerprint=sha256,
    )


def validate_preflight(preflight: DatabasePreflight) -> None:
    if preflight.integrity_check != "ok":
        raise MigrationError("database integrity_check is not ok")
    if preflight.foreign_key_violations:
        raise MigrationError("database has foreign-key violations")
    if preflight.version == 0 and preflight.user_tables:
        raise MigrationError("dirty v0 database is not eligible for bootstrap")
    if preflight.version not in {0, *SUPPORTED_LEGACY_VERSIONS, LATEST_SCHEMA_VERSION}:
        relation = "future" if preflight.version > LATEST_SCHEMA_VERSION else "unsupported"
        raise MigrationError(f"{relation} schema version: {preflight.version}")
    migrations = set(preflight.migration_ids)
    if preflight.version < 5 and MIGRATION_0005 in migrations:
        raise MigrationError("migration record conflicts with user_version")
    if preflight.version < 6 and MIGRATION_0006 in migrations:
        raise MigrationError("migration record conflicts with user_version")
    if preflight.version == 5 and MIGRATION_0005 not in migrations:
        raise MigrationError("v5 database is missing its migration record")
    if preflight.version == 6 and MIGRATION_0006 not in migrations:
        raise MigrationError("v6 database is missing its migration record")


def create_verified_backup(
    db_path: Path | str,
    data_root: Path | str,
    *,
    label: str | None = None,
    now: datetime | None = None,
) -> BackupManifest:
    """Create a verified backup without migrating or creating binding state."""

    source = Path(db_path)
    root = Path(data_root)
    preflight = inspect_database(source)
    validate_preflight(preflight)
    if not preflight.exists:
        raise BackupError("cannot back up an absent database")

    timestamp = (now or _utc_now()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_label = "".join(ch for ch in (label or f"v{preflight.version}") if ch.isalnum() or ch in "-_")
    backups = root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    final_path = backups / f"{source.stem}.{safe_label}.{timestamp}.db"
    temp_path = backups / f".{final_path.name}.{os.getpid()}.tmp"
    if temp_path.exists():
        raise BackupError("backup temporary target already exists")

    try:
        src = _readonly_connection(source)
        dst = sqlite3.connect(temp_path)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        with temp_path.open("r+b") as stream:
            os.fsync(stream.fileno())
        check = _readonly_connection(temp_path)
        try:
            integrity = "\n".join(
                str(row[0]) for row in check.execute("PRAGMA integrity_check").fetchall()
            )
            foreign_keys = len(check.execute("PRAGMA foreign_key_check").fetchall())
            version = int(check.execute("PRAGMA user_version").fetchone()[0])
        finally:
            check.close()
        if integrity != "ok" or foreign_keys or version != preflight.version:
            raise BackupError("backup verification failed")
        os.replace(temp_path, final_path)
        sha256, byte_length = _file_sha256(final_path)
        target_identity = _path_identity(final_path)
        relative = final_path.relative_to(root).as_posix()
        manifest = BackupManifest(
            manifest_version=1,
            source_schema_version=preflight.version,
            integrity_check=integrity,
            foreign_key_violations=foreign_keys,
            sha256=sha256,
            byte_length=byte_length,
            source_identity_hash=preflight.database_identity_hash,
            target_identity_hash=target_identity,
            target_relative_path=relative,
            created_at=(now or _utc_now()).astimezone(timezone.utc).isoformat(),
        )
        manifest_path = final_path.with_suffix(final_path.suffix + ".manifest.json")
        manifest_tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        with manifest_tmp.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(manifest.canonical_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(manifest_tmp, manifest_path)
        return manifest
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def restore_verified_backup(
    manifest: BackupManifest,
    data_root: Path | str,
    restore_path: Path | str,
) -> DatabasePreflight:
    """Restore a verified recovery point to a new, explicit database path.

    The working database is never touched and an existing restore target is
    never overwritten.  This is intentionally separate from normal bootstrap.
    """

    root = Path(data_root).resolve()
    source = (root / Path(manifest.target_relative_path)).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise BackupError("backup manifest path escapes the selected data root") from exc
    if not source.is_file():
        raise BackupError("verified backup file is missing")
    if _path_identity(source) != manifest.target_identity_hash:
        raise BackupError("backup target identity does not match its manifest")
    sidecar = source.with_suffix(source.suffix + ".manifest.json")
    try:
        sidecar_payload = sidecar.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BackupError("backup manifest sidecar is missing") from exc
    if sidecar_payload != manifest.canonical_json():
        raise BackupError("backup manifest sidecar does not match the supplied manifest")
    digest, byte_length = _file_sha256(source)
    if digest != manifest.sha256 or byte_length != manifest.byte_length:
        raise BackupError("backup bytes do not match the verified manifest")
    source_preflight = inspect_database(source)
    validate_preflight(source_preflight)
    if source_preflight.version != manifest.source_schema_version:
        raise BackupError("backup schema version does not match its manifest")

    target = Path(restore_path).resolve()
    if target == source or target.exists():
        raise BackupError("restore target must be a new path separate from the backup")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.restore.tmp")
    if temporary.exists():
        raise BackupError("restore temporary target already exists")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        restored_hash, restored_length = _file_sha256(temporary)
        if restored_hash != manifest.sha256 or restored_length != manifest.byte_length:
            raise BackupError("restored bytes do not match the verified backup")
        restored_preflight = inspect_database(temporary)
        validate_preflight(restored_preflight)
        if restored_preflight.version != manifest.source_schema_version:
            raise BackupError("restored schema version does not match the recovery point")
        os.replace(temporary, target)
        return inspect_database(target)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def open_legacy_v5_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open a frozen v5 recovery copy without migration or write capability."""

    path = Path(db_path)
    preflight = inspect_database(path)
    validate_preflight(preflight)
    if preflight.version != 5:
        raise MigrationError(
            f"legacy v5 reader refuses schema v{preflight.version}"
        )
    return _readonly_connection(path)


LEGACY_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS reports (
        report_id TEXT PRIMARY KEY, parent_report_id TEXT, ticker TEXT NOT NULL,
        version INTEGER NOT NULL, created_at TEXT NOT NULL,
        data_snapshot_id TEXT NOT NULL, method_bundle_id TEXT NOT NULL,
        status TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(ticker, version))""",
    "CREATE INDEX IF NOT EXISTS idx_reports_ticker ON reports(ticker, version DESC)",
    "CREATE TABLE IF NOT EXISTS sources (source_id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS facts (
        fact_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, metric_id TEXT NOT NULL,
        as_of TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_facts_ticker_metric ON facts(ticker, metric_id, as_of DESC)",
    "CREATE TABLE IF NOT EXISTS documents (document_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, payload TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS sync_results (
        sync_result_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, as_of TEXT NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_sync_results_ticker ON sync_results(ticker, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS verification_records (
        verification_id TEXT PRIMARY KEY, sync_result_id TEXT,
        left_fact_id TEXT NOT NULL, right_fact_id TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(sync_result_id) REFERENCES sync_results(sync_result_id))""",
    "CREATE INDEX IF NOT EXISTS idx_verification_left ON verification_records(left_fact_id)",
    "CREATE INDEX IF NOT EXISTS idx_verification_right ON verification_records(right_fact_id)",
    """CREATE TABLE IF NOT EXISTS dimensional_facts (
        dimensional_fact_id TEXT PRIMARY KEY, ticker TEXT NOT NULL,
        metric_id TEXT NOT NULL, dimension_type TEXT NOT NULL, period_end TEXT,
        available_at TEXT NOT NULL, data_snapshot_id TEXT NOT NULL,
        verification_status TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_dimensional_facts_lookup ON dimensional_facts(ticker, metric_id, dimension_type, period_end DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dimensional_facts_snapshot ON dimensional_facts(data_snapshot_id)",
    "CREATE INDEX IF NOT EXISTS idx_dimensional_facts_verification ON dimensional_facts(ticker, verification_status, available_at DESC)",
    """CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, event_type TEXT NOT NULL,
        root_event_id TEXT NOT NULL, canonical_key TEXT NOT NULL,
        lifecycle_state TEXT NOT NULL, announced_at TEXT NOT NULL,
        available_at TEXT NOT NULL, data_snapshot_id TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_events_lookup ON events(ticker, event_type, announced_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_root ON events(root_event_id, announced_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_snapshot ON events(data_snapshot_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_canonical ON events(canonical_key)",
    """CREATE TABLE IF NOT EXISTS industry_facts (
        industry_fact_id TEXT PRIMARY KEY, industry_code TEXT NOT NULL,
        metric_id TEXT NOT NULL, period_end TEXT, available_at TEXT NOT NULL,
        dataset_version TEXT NOT NULL, data_snapshot_id TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_industry_facts_lookup ON industry_facts(industry_code, metric_id, period_end DESC)",
    "CREATE INDEX IF NOT EXISTS idx_industry_facts_snapshot ON industry_facts(data_snapshot_id)",
    """CREATE TABLE IF NOT EXISTS forecast_snapshots (
        forecast_snapshot_id TEXT PRIMARY KEY, ticker TEXT NOT NULL,
        provider TEXT NOT NULL, metric_id TEXT NOT NULL, target_period TEXT NOT NULL,
        as_of TEXT NOT NULL, available_at TEXT NOT NULL,
        data_snapshot_id TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_lookup ON forecast_snapshots(ticker, metric_id, target_period, as_of DESC)",
    "CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_snapshot ON forecast_snapshots(data_snapshot_id)",
    """CREATE TABLE IF NOT EXISTS peer_set_versions (
        peer_set_id TEXT NOT NULL, version INTEGER NOT NULL,
        target_ticker TEXT NOT NULL, as_of TEXT NOT NULL,
        data_snapshot_id TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(peer_set_id, version))""",
    "CREATE INDEX IF NOT EXISTS idx_peer_set_versions_lookup ON peer_set_versions(target_ticker, as_of DESC)",
    "CREATE INDEX IF NOT EXISTS idx_peer_set_versions_snapshot ON peer_set_versions(data_snapshot_id)",
    """CREATE TABLE IF NOT EXISTS announcements (
        announcement_record_id TEXT PRIMARY KEY, ticker TEXT NOT NULL,
        provider TEXT NOT NULL, announcement_id TEXT NOT NULL,
        canonical_key TEXT NOT NULL, announced_at TEXT NOT NULL,
        available_at TEXT NOT NULL, classified_event_type TEXT,
        data_snapshot_id TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_announcements_lookup ON announcements(ticker, announced_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_announcements_canonical ON announcements(canonical_key)",
    "CREATE INDEX IF NOT EXISTS idx_announcements_event_type ON announcements(ticker, classified_event_type, announced_at DESC)",
)


ACQUISITION_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS storage_namespaces (
        namespace_id TEXT PRIMARY KEY, binding_nonce TEXT NOT NULL UNIQUE,
        layout_version INTEGER NOT NULL, database_identity_hash TEXT NOT NULL UNIQUE,
        data_root_identity_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS source_registry_versions (
        registry_id TEXT NOT NULL, registry_version TEXT NOT NULL,
        content_hash TEXT NOT NULL, question_set_version TEXT,
        effective_at TEXT, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(registry_id, registry_version), UNIQUE(content_hash))""",
    """CREATE TABLE IF NOT EXISTS source_definition_versions (
        source_definition_id TEXT NOT NULL, source_definition_version TEXT NOT NULL,
        registry_id TEXT, registry_version TEXT, upstream_identity TEXT,
        policy_status TEXT, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(source_definition_id, source_definition_version),
        FOREIGN KEY(registry_id, registry_version)
            REFERENCES source_registry_versions(registry_id, registry_version))""",
    "CREATE INDEX IF NOT EXISTS idx_source_definitions_registry ON source_definition_versions(registry_id, registry_version, source_definition_id)",
    """CREATE TABLE IF NOT EXISTS source_candidates (
        candidate_id TEXT PRIMARY KEY, candidate_domain TEXT, review_status TEXT NOT NULL,
        discovered_at TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_source_candidates_status ON source_candidates(review_status, discovered_at, candidate_id)",
    """CREATE TABLE IF NOT EXISTS acquisition_runs (
        run_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, mode TEXT NOT NULL,
        run_kind TEXT NOT NULL, as_of TEXT NOT NULL, registry_id TEXT,
        registry_version TEXT, registry_hash TEXT NOT NULL,
        question_set_version TEXT NOT NULL, question_set_hash TEXT,
        parent_run_id TEXT, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(parent_run_id) REFERENCES acquisition_runs(run_id),
        FOREIGN KEY(registry_id, registry_version)
            REFERENCES source_registry_versions(registry_id, registry_version))""",
    "CREATE INDEX IF NOT EXISTS idx_acquisition_runs_lookup ON acquisition_runs(ticker, run_kind, created_at DESC, run_id)",
    """CREATE TABLE IF NOT EXISTS acquisition_run_events (
        event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_type TEXT NOT NULL,
        lease_epoch INTEGER, occurred_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id))""",
    "CREATE INDEX IF NOT EXISTS idx_run_events_order ON acquisition_run_events(run_id, occurred_at, event_id)",
    """CREATE TABLE IF NOT EXISTS acquisition_execution_leases (
        run_id TEXT PRIMARY KEY, owner_token_hash TEXT NOT NULL,
        lease_epoch INTEGER NOT NULL CHECK(lease_epoch > 0),
        acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL, released_at TEXT,
        FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id))""",
    """CREATE TABLE IF NOT EXISTS physical_query_plan_items (
        plan_item_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
        source_definition_id TEXT NOT NULL, source_definition_version TEXT NOT NULL,
        query_id TEXT NOT NULL, execution_key TEXT NOT NULL,
        partition_key TEXT, pagination_fingerprint TEXT NOT NULL,
        time_start TEXT, time_end TEXT,
        ordinal INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id),
        FOREIGN KEY(source_definition_id, source_definition_version)
            REFERENCES source_definition_versions(source_definition_id, source_definition_version),
        UNIQUE(run_id, execution_key, partition_key, time_start, time_end))""",
    "CREATE INDEX IF NOT EXISTS idx_plan_run_order ON physical_query_plan_items(run_id, ordinal, plan_item_id)",
    """CREATE TABLE IF NOT EXISTS coverage_entries (
        coverage_entry_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
        source_definition_id TEXT NOT NULL, source_definition_version TEXT NOT NULL,
        question_id TEXT NOT NULL, query_id TEXT NOT NULL,
        time_start TEXT, time_end TEXT, ordinal INTEGER NOT NULL DEFAULT 0,
        payload TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id),
        FOREIGN KEY(source_definition_id, source_definition_version)
            REFERENCES source_definition_versions(source_definition_id, source_definition_version),
        UNIQUE(run_id, source_definition_id, source_definition_version,
               question_id, query_id, time_start, time_end))""",
    "CREATE INDEX IF NOT EXISTS idx_coverage_run_order ON coverage_entries(run_id, ordinal, coverage_entry_id)",
    """CREATE TABLE IF NOT EXISTS physical_query_coverage_links (
        plan_item_id TEXT NOT NULL, coverage_entry_id TEXT NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(plan_item_id, coverage_entry_id),
        FOREIGN KEY(plan_item_id) REFERENCES physical_query_plan_items(plan_item_id),
        FOREIGN KEY(coverage_entry_id) REFERENCES coverage_entries(coverage_entry_id))""",
    "CREATE INDEX IF NOT EXISTS idx_coverage_links_reverse ON physical_query_coverage_links(coverage_entry_id, plan_item_id)",
    """CREATE TABLE IF NOT EXISTS coverage_resolutions (
        resolution_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
        coverage_entry_id TEXT NOT NULL, resolution_status TEXT NOT NULL,
        lease_epoch INTEGER, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id),
        FOREIGN KEY(coverage_entry_id) REFERENCES coverage_entries(coverage_entry_id))""",
    "CREATE INDEX IF NOT EXISTS idx_coverage_resolutions ON coverage_resolutions(run_id, coverage_entry_id, created_at, resolution_id)",
    """CREATE TABLE IF NOT EXISTS acquisition_attempts (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
        physical_query_plan_item_id TEXT NOT NULL,
        source_definition_id TEXT NOT NULL, source_definition_version TEXT NOT NULL,
        attempt_kind TEXT NOT NULL, execution_key TEXT NOT NULL,
        retry_group_id TEXT NOT NULL, retry_ordinal INTEGER NOT NULL,
        work_position TEXT NOT NULL,
        supersedes_attempt_id TEXT, lease_epoch INTEGER NOT NULL,
        started_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id),
        FOREIGN KEY(physical_query_plan_item_id) REFERENCES physical_query_plan_items(plan_item_id),
        FOREIGN KEY(supersedes_attempt_id) REFERENCES acquisition_attempts(attempt_id),
        UNIQUE(retry_group_id, retry_ordinal),
        CHECK(supersedes_attempt_id IS NULL OR supersedes_attempt_id <> attempt_id))""",
    "CREATE INDEX IF NOT EXISTS idx_attempts_run_plan ON acquisition_attempts(run_id, physical_query_plan_item_id, started_at, attempt_id)",
    """CREATE TABLE IF NOT EXISTS acquisition_attempt_events (
        event_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
        event_type TEXT NOT NULL, outcome TEXT, reason_code TEXT,
        lease_epoch INTEGER NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES acquisition_attempts(attempt_id))""",
    "CREATE INDEX IF NOT EXISTS idx_attempt_events_order ON acquisition_attempt_events(attempt_id, occurred_at, event_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_outcome_terminal ON acquisition_attempt_events(attempt_id) WHERE outcome IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_abandoned ON acquisition_attempt_events(attempt_id) WHERE event_type='abandoned'",
    """CREATE TRIGGER IF NOT EXISTS trg_attempt_lifecycle_exclusive
        BEFORE INSERT ON acquisition_attempt_events
        WHEN (NEW.outcome IS NOT NULL AND EXISTS(
                SELECT 1 FROM acquisition_attempt_events
                WHERE attempt_id=NEW.attempt_id AND event_type='abandoned'))
          OR (NEW.event_type='abandoned' AND EXISTS(
                SELECT 1 FROM acquisition_attempt_events
                WHERE attempt_id=NEW.attempt_id AND outcome IS NOT NULL))
        BEGIN SELECT RAISE(ABORT, 'attempt outcome and abandoned are exclusive'); END""",
    """CREATE TABLE IF NOT EXISTS acquisition_attempt_segments (
        segment_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
        segment_ordinal INTEGER NOT NULL, work_position TEXT NOT NULL,
        lease_epoch INTEGER NOT NULL, committed_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES acquisition_attempts(attempt_id),
        UNIQUE(attempt_id, segment_ordinal))""",
    "CREATE INDEX IF NOT EXISTS idx_attempt_segments_position ON acquisition_attempt_segments(attempt_id, segment_ordinal, segment_id)",
    """CREATE TABLE IF NOT EXISTS discovery_observations (
        observation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
        physical_query_plan_item_id TEXT NOT NULL, snapshot_id TEXT,
        page_ordinal INTEGER, cursor TEXT, observed_at TEXT NOT NULL,
        retrieved_at TEXT, lease_epoch INTEGER NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES acquisition_attempts(attempt_id),
        FOREIGN KEY(physical_query_plan_item_id) REFERENCES physical_query_plan_items(plan_item_id),
        FOREIGN KEY(snapshot_id) REFERENCES raw_resource_snapshots(snapshot_id)
            DEFERRABLE INITIALLY DEFERRED)""",
    "CREATE INDEX IF NOT EXISTS idx_discovery_observations ON discovery_observations(attempt_id, page_ordinal, observation_id)",
    """CREATE TABLE IF NOT EXISTS discovery_proofs (
        proof_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL,
        response_sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL,
        schema_version TEXT NOT NULL, terminal_proof INTEGER NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(observation_id) REFERENCES discovery_observations(observation_id))""",
    """CREATE TABLE IF NOT EXISTS discovered_resources (
        discovered_resource_id TEXT PRIMARY KEY, proof_id TEXT,
        discovery_observation_id TEXT NOT NULL, canonical_resource_id TEXT NOT NULL,
        row_hash TEXT NOT NULL, row_ordinal INTEGER, required_fetch INTEGER NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(proof_id) REFERENCES discovery_proofs(proof_id),
        FOREIGN KEY(discovery_observation_id) REFERENCES discovery_observations(observation_id))""",
    "CREATE INDEX IF NOT EXISTS idx_discovered_resources_canonical ON discovered_resources(canonical_resource_id, discovery_observation_id)",
    """CREATE TABLE IF NOT EXISTS source_checkpoints (
        checkpoint_id TEXT PRIMARY KEY, ticker TEXT NOT NULL,
        source_definition_id TEXT NOT NULL, source_definition_version TEXT NOT NULL,
        question_set_version TEXT NOT NULL, checkpoint_version INTEGER NOT NULL,
        parent_checkpoint_id TEXT, safe_through TEXT, source_lower_bound TEXT,
        run_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(parent_checkpoint_id) REFERENCES source_checkpoints(checkpoint_id),
        FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id),
        UNIQUE(ticker, source_definition_id, source_definition_version,
               question_set_version, checkpoint_version))""",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_latest ON source_checkpoints(ticker, source_definition_id, source_definition_version, question_set_version, checkpoint_version DESC)",
    """CREATE TABLE IF NOT EXISTS checkpoint_barriers (
        barrier_id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL,
        source_definition_id TEXT NOT NULL, source_definition_version TEXT NOT NULL,
        partition_key TEXT NOT NULL, work_position TEXT NOT NULL,
        canonical_resource_id TEXT, retry_group_id TEXT,
        opening_attempt_id TEXT NOT NULL, query_semantics_hash TEXT NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(checkpoint_id) REFERENCES source_checkpoints(checkpoint_id),
        FOREIGN KEY(opening_attempt_id) REFERENCES acquisition_attempts(attempt_id))""",
    "CREATE INDEX IF NOT EXISTS idx_checkpoint_barriers_position ON checkpoint_barriers(source_definition_id, partition_key, work_position, barrier_id)",
    """CREATE TABLE IF NOT EXISTS barrier_resolutions (
        barrier_resolution_id TEXT PRIMARY KEY, barrier_id TEXT NOT NULL UNIQUE,
        resolving_attempt_id TEXT NOT NULL, source_definition_id TEXT NOT NULL,
        source_definition_version TEXT NOT NULL, partition_key TEXT NOT NULL,
        work_position TEXT NOT NULL, canonical_resource_id TEXT,
        query_semantics_hash TEXT, proof_id TEXT, snapshot_id TEXT,
        observation_id TEXT, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(barrier_id) REFERENCES checkpoint_barriers(barrier_id),
        FOREIGN KEY(resolving_attempt_id) REFERENCES acquisition_attempts(attempt_id),
        FOREIGN KEY(proof_id) REFERENCES discovery_proofs(proof_id))""",
    """CREATE TABLE IF NOT EXISTS content_blobs (
        content_blob_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
        byte_length INTEGER NOT NULL, relative_path TEXT NOT NULL,
        storage_namespace_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(storage_namespace_id) REFERENCES storage_namespaces(namespace_id))""",
    """CREATE TABLE IF NOT EXISTS raw_resource_snapshots (
        snapshot_id TEXT PRIMARY KEY, resource_role TEXT NOT NULL,
        source_definition_id TEXT NOT NULL, source_definition_version TEXT NOT NULL,
        creating_observation_id TEXT NOT NULL, content_blob_id TEXT NOT NULL,
        canonical_resource_id TEXT, upstream_material_id TEXT,
        canonical_url TEXT, physical_query_plan_item_id TEXT,
        query_page_canonical TEXT, page_ordinal INTEGER, cursor TEXT,
        content_sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL,
        available_at TEXT NOT NULL, archived_relative_path TEXT NOT NULL,
        storage_namespace_id TEXT NOT NULL, version INTEGER NOT NULL,
        supersedes_snapshot_id TEXT, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(content_blob_id) REFERENCES content_blobs(content_blob_id),
        FOREIGN KEY(storage_namespace_id) REFERENCES storage_namespaces(namespace_id),
        FOREIGN KEY(supersedes_snapshot_id) REFERENCES raw_resource_snapshots(snapshot_id),
        FOREIGN KEY(physical_query_plan_item_id) REFERENCES physical_query_plan_items(plan_item_id),
        CHECK(resource_role IN ('content','discovery_response')),
        CHECK((resource_role='content' AND canonical_resource_id IS NOT NULL
               AND upstream_material_id IS NOT NULL AND canonical_url IS NOT NULL
               AND physical_query_plan_item_id IS NULL AND query_page_canonical IS NULL)
           OR (resource_role='discovery_response' AND canonical_resource_id IS NULL
               AND upstream_material_id IS NULL AND canonical_url IS NULL
               AND physical_query_plan_item_id IS NOT NULL AND query_page_canonical IS NOT NULL)),
        UNIQUE(source_definition_id, source_definition_version,
               resource_role, canonical_resource_id, query_page_canonical, version))""",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_content_lookup ON raw_resource_snapshots(source_definition_id, source_definition_version, canonical_resource_id, version DESC)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_discovery_lookup ON raw_resource_snapshots(source_definition_id, source_definition_version, query_page_canonical, version DESC)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_hash ON raw_resource_snapshots(content_sha256, snapshot_id)",
    """CREATE TABLE IF NOT EXISTS resource_observations (
        observation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
        discovered_resource_id TEXT, source_definition_id TEXT NOT NULL,
        source_definition_version TEXT NOT NULL, snapshot_id TEXT,
        disposition TEXT, http_status INTEGER, etag TEXT, last_modified TEXT,
        validator_snapshot_id TEXT, observed_at TEXT NOT NULL, retrieved_at TEXT,
        reason_code TEXT, lease_epoch INTEGER NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES acquisition_attempts(attempt_id),
        FOREIGN KEY(discovered_resource_id) REFERENCES discovered_resources(discovered_resource_id),
        FOREIGN KEY(snapshot_id) REFERENCES raw_resource_snapshots(snapshot_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(validator_snapshot_id) REFERENCES raw_resource_snapshots(snapshot_id))""",
    "CREATE INDEX IF NOT EXISTS idx_resource_observations ON resource_observations(attempt_id, observed_at, observation_id)",
    "CREATE INDEX IF NOT EXISTS idx_resource_observations_snapshot ON resource_observations(snapshot_id, observed_at)",
    """CREATE TABLE IF NOT EXISTS snapshot_integrity_events (
        integrity_event_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
        integrity_status TEXT NOT NULL, checked_at TEXT NOT NULL,
        reason_code TEXT, payload TEXT NOT NULL,
        FOREIGN KEY(snapshot_id) REFERENCES raw_resource_snapshots(snapshot_id))""",
    "CREATE INDEX IF NOT EXISTS idx_integrity_events_snapshot ON snapshot_integrity_events(snapshot_id, checked_at, integrity_event_id)",
    """CREATE TABLE IF NOT EXISTS derived_artifacts (
        derived_artifact_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
        extractor_id TEXT NOT NULL, extractor_version TEXT NOT NULL,
        output_hash TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(snapshot_id) REFERENCES raw_resource_snapshots(snapshot_id),
        UNIQUE(snapshot_id, extractor_id, extractor_version, output_hash))""",
    """CREATE TABLE IF NOT EXISTS evidence_manifests (
        manifest_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
        manifest_hash TEXT NOT NULL UNIQUE, as_of TEXT NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id))""",
    """CREATE TABLE IF NOT EXISTS evidence_manifest_items (
        manifest_id TEXT NOT NULL, item_type TEXT NOT NULL, item_id TEXT NOT NULL,
        disposition TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(manifest_id, item_type, item_id),
        FOREIGN KEY(manifest_id) REFERENCES evidence_manifests(manifest_id))""",
    "CREATE INDEX IF NOT EXISTS idx_manifest_items_order ON evidence_manifest_items(manifest_id, ordinal, item_type, item_id)",
)


def _execute_statements(
    connection: sqlite3.Connection,
    statements: Iterable[str],
    *,
    fault: Callable[[str], None] | None,
    stage: str,
) -> None:
    for index, statement in enumerate(statements, start=1):
        connection.execute(statement)
        if fault is not None:
            fault(f"{stage}:{index}")


def _create_documents_fts(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='documents_fts'"
    ).fetchone()
    if existing is not None:
        return
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE documents_fts USING fts5("
            "document_id UNINDEXED, ticker UNINDEXED, title, body, tokenize='unicode61')"
        )
    except sqlite3.OperationalError:
        connection.execute(
            "CREATE TABLE documents_fts("
            "document_id TEXT PRIMARY KEY, ticker TEXT, title TEXT, body TEXT)"
        )


class MigrationCoordinator:
    """Explicit v0/v4/v5/v6 migration matrix with verified recovery points."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        busy_timeout_ms: int = 5_000,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.fault_injector = fault_injector

    def preflight(self) -> DatabasePreflight:
        result = inspect_database(self.db_path)
        validate_preflight(result)
        return result

    def migrate(
        self,
        *,
        data_root: Path | str | None = None,
        backup_callback: Callable[[int, BackupManifest], None] | None = None,
    ) -> tuple[BackupManifest, ...]:
        preflight = self.preflight()
        if preflight.version == LATEST_SCHEMA_VERSION:
            return ()
        if preflight.version in SUPPORTED_LEGACY_VERSIONS and data_root is None:
            raise MigrationError("legacy migration requires an explicit backup data root")

        backups: list[BackupManifest] = []
        if preflight.version == 0:
            self._bootstrap_fresh()
            return ()

        if preflight.version == 4:
            backup_v4 = create_verified_backup(self.db_path, data_root, label="pre-v5")
            backups.append(backup_v4)
            if backup_callback:
                backup_callback(4, backup_v4)
            self._migrate_0005()

        current = self.preflight()
        if current.version != 5:
            raise MigrationError("migration did not reach the required v5 recovery point")
        backup_v5 = create_verified_backup(self.db_path, data_root, label="recovery-v5")
        backups.append(backup_v5)
        if backup_callback:
            backup_callback(5, backup_v5)
        self._migrate_0006()
        return tuple(backups)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=max(self.busy_timeout_ms, 1) / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    def _transaction(self, operation: Callable[[sqlite3.Connection], None]) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            operation(connection)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _bootstrap_fresh(self) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 0:
                raise MigrationError("database version changed after preflight")
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                raise MigrationError("dirty v0 database is not eligible for bootstrap")
            _execute_statements(
                connection,
                LEGACY_SCHEMA_STATEMENTS,
                fault=self.fault_injector,
                stage="fresh_legacy",
            )
            _create_documents_fts(connection)
            _execute_statements(
                connection,
                ACQUISITION_SCHEMA_STATEMENTS,
                fault=self.fault_injector,
                stage="0006",
            )
            applied_at = _utc_now().isoformat()
            for migration_id in (
                "0002_research_data_contracts",
                "0003_announcement_index",
                "0004_event_snapshot_versions",
                MIGRATION_0005,
                MIGRATION_0006,
            ):
                connection.execute(
                    "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                    (migration_id, applied_at),
                )
            if self.fault_injector:
                self.fault_injector("fresh_before_version")
            connection.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")

        self._transaction(operation)

    def bootstrap_fresh(self) -> None:
        preflight = self.preflight()
        if preflight.version != 0:
            raise MigrationError("fresh bootstrap requires an empty v0 database")
        self._bootstrap_fresh()

    def _migrate_0005(self) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 4:
                raise MigrationError("0005 requires schema v4")
            _execute_statements(
                connection,
                LEGACY_SCHEMA_STATEMENTS[:12],
                fault=self.fault_injector,
                stage="0005_legacy",
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(dimensional_facts)")
            }
            if "verification_status" not in columns:
                connection.execute(
                    "ALTER TABLE dimensional_facts ADD COLUMN verification_status TEXT"
                )
            for row in connection.execute(
                "SELECT dimensional_fact_id, payload FROM dimensional_facts "
                "WHERE verification_status IS NULL OR verification_status=''"
            ).fetchall():
                try:
                    payload = json.loads(row[1])
                    status = str(payload.get("verification_status") or "未核验")
                except (TypeError, ValueError) as exc:
                    raise MigrationError("invalid legacy dimensional fact payload") from exc
                connection.execute(
                    "UPDATE dimensional_facts SET verification_status=? "
                    "WHERE dimensional_fact_id=?",
                    (status, row[0]),
                )
            if self.fault_injector:
                self.fault_injector("0005_after_backfill")
            _execute_statements(
                connection,
                LEGACY_SCHEMA_STATEMENTS[12:],
                fault=self.fault_injector,
                stage="0005_schema",
            )
            _create_documents_fts(connection)
            connection.execute(
                "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                (MIGRATION_0005, _utc_now().isoformat()),
            )
            connection.execute("PRAGMA user_version=5")

        self._transaction(operation)

    def apply_0005(self) -> None:
        preflight = self.preflight()
        if preflight.version != 4:
            raise MigrationError("0005 requires schema v4")
        self._migrate_0005()

    def _migrate_0006(self) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 5:
                raise MigrationError("0006 requires schema v5")
            existing = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_id=?",
                (MIGRATION_0006,),
            ).fetchone()
            if existing is not None:
                raise MigrationError("0006 was already recorded")
            _execute_statements(
                connection,
                ACQUISITION_SCHEMA_STATEMENTS,
                fault=self.fault_injector,
                stage="0006",
            )
            connection.execute(
                "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                (MIGRATION_0006, _utc_now().isoformat()),
            )
            if self.fault_injector:
                self.fault_injector("0006_before_version")
            connection.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")

        self._transaction(operation)

    def apply_0006(self) -> None:
        preflight = self.preflight()
        if preflight.version != 5:
            raise MigrationError("0006 requires schema v5")
        self._migrate_0006()


def migrate_database(
    db_path: Path | str,
    *,
    data_root: Path | str | None = None,
    busy_timeout_ms: int = 5_000,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[BackupManifest, ...]:
    return MigrationCoordinator(
        db_path,
        busy_timeout_ms=busy_timeout_ms,
        fault_injector=fault_injector,
    ).migrate(data_root=data_root)
