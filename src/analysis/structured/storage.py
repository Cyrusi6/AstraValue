from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from analysis.acquisition.bootstrap import InterProcessFileLock
from analysis.acquisition.migrations import (
    LATEST_SCHEMA_VERSION,
    BackupManifest,
    create_verified_backup,
    inspect_database,
    validate_preflight,
)
from analysis.acquisition.repository import (
    AcquisitionNotFoundError,
    AcquisitionStorageError,
    ImmutableRecordError,
)


MODULE_MIGRATION_ID = "structured_data_0001"
MODULE_SCHEMA_VERSION = 1
STRUCTURED_SCOPE = "structured_data"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class StructuredStorageError(RuntimeError):
    """The structured-data module cannot safely use the bound database."""


class StructuredSchemaError(StructuredStorageError):
    """The module schema or migration journal is incomplete or contradictory."""


class StructuredNamespaceMismatch(StructuredStorageError):
    """A structured object crossed the database's frozen storage namespace."""


@dataclass(frozen=True)
class StructuredRunContext:
    """Frozen structured-data identity attached to one shared acquisition run."""

    run_id: str
    storage_namespace_id: str
    ticker: str
    company_id: str
    dataset_registry_id: str
    dataset_registry_version: str
    dataset_registry_hash: str
    field_registry_id: str
    field_registry_version: str
    field_registry_hash: str
    requirement_set_id: str
    requirement_set_version: str
    requirement_set_hash: str
    query_pack_id: str
    query_pack_version: str
    query_pack_hash: str
    source_registry_id: str
    source_registry_version: str
    source_registry_hash: str
    schedule_id: str
    schedule_version: str
    schedule_hash: str
    policy_version: str
    frozen_config: Mapping[str, Any]
    peer_set_id: str | None = None
    peer_set_version: str | None = None
    peer_set_hash: str | None = None
    acquisition_scope: str = STRUCTURED_SCOPE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        required = (
            "run_id",
            "storage_namespace_id",
            "ticker",
            "company_id",
            "dataset_registry_id",
            "dataset_registry_version",
            "field_registry_id",
            "field_registry_version",
            "requirement_set_id",
            "requirement_set_version",
            "query_pack_id",
            "query_pack_version",
            "source_registry_id",
            "source_registry_version",
            "schedule_id",
            "schedule_version",
            "policy_version",
        )
        if any(not str(getattr(self, name)).strip() for name in required):
            raise ValueError("StructuredRunContext identity fields must be non-empty")
        if self.acquisition_scope != STRUCTURED_SCOPE:
            raise ValueError("StructuredRunContext acquisition_scope must be structured_data")
        for name in (
            "dataset_registry_hash",
            "field_registry_hash",
            "requirement_set_hash",
            "query_pack_hash",
            "source_registry_hash",
            "schedule_hash",
        ):
            _require_sha256(str(getattr(self, name)), name)
        peer_values = (self.peer_set_id, self.peer_set_version, self.peer_set_hash)
        if any(value is not None for value in peer_values) and not all(peer_values):
            raise ValueError("peer-set identity must be wholly present or wholly absent")
        if self.peer_set_hash is not None:
            _require_sha256(self.peer_set_hash, "peer_set_hash")
        object.__setattr__(self, "created_at", _aware_utc(self.created_at))
        # Canonicalize eagerly so mutable/non-JSON runtime objects cannot become
        # an apparently frozen execution context.
        object.__setattr__(self, "frozen_config", _json_round_trip(self.frozen_config))

    @property
    def content_hash(self) -> str:
        return canonical_sha256(asdict(self))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StructuredRunContext":
        data = dict(value)
        data.pop("context_hash", None)
        if isinstance(data.get("created_at"), str):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**data)


@dataclass(frozen=True)
class ModuleMigrationResult:
    migration_id: str
    schema_version: int
    applied: bool
    backup: BackupManifest | None
    user_version: int


STRUCTURED_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE structured_schema_migrations (
        migration_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
        schema_hash TEXT NOT NULL, applied_at TEXT NOT NULL)""",
    """CREATE TABLE structured_run_contexts (
        run_id TEXT PRIMARY KEY, storage_namespace_id TEXT NOT NULL,
        acquisition_scope TEXT NOT NULL CHECK(acquisition_scope='structured_data'),
        ticker TEXT NOT NULL, company_id TEXT NOT NULL,
        dataset_registry_id TEXT NOT NULL, dataset_registry_version TEXT NOT NULL,
        dataset_registry_hash TEXT NOT NULL, field_registry_id TEXT NOT NULL,
        field_registry_version TEXT NOT NULL, field_registry_hash TEXT NOT NULL,
        requirement_set_id TEXT NOT NULL, requirement_set_version TEXT NOT NULL,
        requirement_set_hash TEXT NOT NULL, query_pack_id TEXT NOT NULL,
        query_pack_version TEXT NOT NULL, query_pack_hash TEXT NOT NULL,
        source_registry_id TEXT NOT NULL, source_registry_version TEXT NOT NULL,
        source_registry_hash TEXT NOT NULL, peer_set_id TEXT, peer_set_version TEXT,
        peer_set_hash TEXT, schedule_id TEXT NOT NULL, schedule_version TEXT NOT NULL,
        schedule_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
        context_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES acquisition_runs(run_id),
        FOREIGN KEY(storage_namespace_id) REFERENCES storage_namespaces(namespace_id),
        FOREIGN KEY(source_registry_id, source_registry_version)
            REFERENCES source_registry_versions(registry_id, registry_version))""",
    "CREATE INDEX idx_structured_context_company ON structured_run_contexts(storage_namespace_id, company_id, ticker, created_at, run_id)",
    """CREATE TABLE structured_jobs (
        job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, plan_item_id TEXT NOT NULL UNIQUE,
        storage_namespace_id TEXT NOT NULL, company_id TEXT NOT NULL, ticker TEXT NOT NULL,
        dataset_id TEXT NOT NULL, source_definition_id TEXT NOT NULL,
        source_definition_version TEXT NOT NULL, purpose TEXT NOT NULL,
        schedule_mode TEXT NOT NULL, scope_key TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
        time_start TEXT, time_end TEXT, max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
        ordinal INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES structured_run_contexts(run_id),
        FOREIGN KEY(plan_item_id) REFERENCES physical_query_plan_items(plan_item_id),
        FOREIGN KEY(storage_namespace_id) REFERENCES storage_namespaces(namespace_id))""",
    "CREATE INDEX idx_structured_jobs_run ON structured_jobs(run_id, ordinal, job_id)",
    "CREATE INDEX idx_structured_jobs_scope ON structured_jobs(storage_namespace_id, company_id, dataset_id, scope_key, created_at)",
    """CREATE TABLE structured_pages (
        page_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL, page_number INTEGER, cursor TEXT, position_key TEXT NOT NULL,
        row_count INTEGER NOT NULL CHECK(row_count >= 0), total_count INTEGER,
        terminal INTEGER NOT NULL CHECK(terminal IN (0,1)), content_hash TEXT NOT NULL,
        lease_epoch INTEGER NOT NULL CHECK(lease_epoch > 0), committed_at TEXT NOT NULL,
        payload TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES structured_jobs(job_id),
        FOREIGN KEY(attempt_id) REFERENCES acquisition_attempts(attempt_id),
        FOREIGN KEY(snapshot_id) REFERENCES raw_resource_snapshots(snapshot_id),
        UNIQUE(job_id, position_key, content_hash))""",
    "CREATE INDEX idx_structured_pages_job ON structured_pages(job_id, committed_at, page_id)",
    """CREATE TABLE structured_records (
        record_version_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, page_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL, row_key TEXT NOT NULL, version_hash TEXT NOT NULL,
        effective_at TEXT, available_at TEXT NOT NULL, observed_at TEXT NOT NULL,
        supersedes_record_version_id TEXT, payload TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES structured_jobs(job_id),
        FOREIGN KEY(page_id) REFERENCES structured_pages(page_id),
        FOREIGN KEY(snapshot_id) REFERENCES raw_resource_snapshots(snapshot_id),
        FOREIGN KEY(supersedes_record_version_id) REFERENCES structured_records(record_version_id),
        UNIQUE(job_id, row_key, version_hash))""",
    "CREATE INDEX idx_structured_records_row ON structured_records(job_id, row_key, available_at, record_version_id)",
    "CREATE INDEX idx_structured_records_snapshot ON structured_records(snapshot_id, record_version_id)",
    """CREATE TABLE structured_record_fields (
        field_value_id TEXT PRIMARY KEY, record_version_id TEXT NOT NULL,
        dataset_id TEXT NOT NULL, raw_field_name TEXT NOT NULL, field_path TEXT NOT NULL,
        standard_field_id TEXT, definition_version TEXT, nature TEXT NOT NULL,
        quality TEXT NOT NULL, value_json TEXT, unit TEXT, period_key TEXT,
        payload TEXT NOT NULL, FOREIGN KEY(record_version_id)
            REFERENCES structured_records(record_version_id),
        UNIQUE(record_version_id, field_path))""",
    "CREATE INDEX idx_structured_fields_standard ON structured_record_fields(standard_field_id, period_key, record_version_id)",
    "CREATE INDEX idx_structured_fields_raw ON structured_record_fields(dataset_id, raw_field_name, record_version_id)",
    """CREATE TABLE structured_acquisition_coverage (
        coverage_record_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
        shared_coverage_entry_id TEXT, job_id TEXT, company_id TEXT NOT NULL,
        dataset_id TEXT NOT NULL, scope_key TEXT NOT NULL, status TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version > 0), safe_through TEXT,
        available_from TEXT, available_to TEXT, reason_code TEXT,
        supersedes_coverage_record_id TEXT, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES structured_run_contexts(run_id),
        FOREIGN KEY(shared_coverage_entry_id) REFERENCES coverage_entries(coverage_entry_id),
        FOREIGN KEY(job_id) REFERENCES structured_jobs(job_id),
        FOREIGN KEY(supersedes_coverage_record_id)
            REFERENCES structured_acquisition_coverage(coverage_record_id),
        UNIQUE(run_id, company_id, dataset_id, scope_key, version))""",
    "CREATE INDEX idx_structured_coverage_scope ON structured_acquisition_coverage(run_id, company_id, dataset_id, scope_key, version)",
    """CREATE TABLE structured_reading_tasks (
        reading_task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, company_id TEXT NOT NULL,
        material_id TEXT NOT NULL, content_version TEXT NOT NULL, report_period TEXT,
        state TEXT NOT NULL, task_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        payload TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES structured_run_contexts(run_id))""",
    "CREATE INDEX idx_structured_reading_state ON structured_reading_tasks(run_id, state, created_at, reading_task_id)",
    """CREATE TABLE structured_research_coverage_snapshots (
        coverage_snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
        storage_namespace_id TEXT NOT NULL, company_id TEXT NOT NULL, ticker TEXT NOT NULL,
        requirement_set_id TEXT NOT NULL, requirement_set_version TEXT NOT NULL,
        requirement_set_hash TEXT NOT NULL, field_registry_version TEXT NOT NULL,
        industry_profile_id TEXT NOT NULL, industry_profile_version TEXT NOT NULL,
        industry_profile_hash TEXT NOT NULL, peer_set_id TEXT, peer_set_version TEXT,
        method_version TEXT, as_of TEXT NOT NULL, analysis_scope_json TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL UNIQUE, evaluated_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES structured_run_contexts(run_id),
        FOREIGN KEY(storage_namespace_id) REFERENCES storage_namespaces(namespace_id))""",
    "CREATE INDEX idx_structured_research_coverage_company ON structured_research_coverage_snapshots(storage_namespace_id, company_id, as_of, evaluated_at, coverage_snapshot_id)",
    """CREATE TABLE structured_requirement_evaluations (
        evaluation_id TEXT PRIMARY KEY, coverage_snapshot_id TEXT NOT NULL,
        company_id TEXT NOT NULL, step_id TEXT NOT NULL, question_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL, period_key TEXT NOT NULL,
        applicability TEXT NOT NULL, readiness TEXT NOT NULL,
        required_group_id TEXT, optional INTEGER NOT NULL CHECK(optional IN (0,1)),
        reason_code TEXT, evaluated_at TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(coverage_snapshot_id)
            REFERENCES structured_research_coverage_snapshots(coverage_snapshot_id),
        UNIQUE(coverage_snapshot_id, requirement_id, period_key))""",
    "CREATE INDEX idx_structured_evaluations_question ON structured_requirement_evaluations(coverage_snapshot_id, step_id, question_id, evaluation_id)",
)


MODULE_SCHEMA_HASH = hashlib.sha256(
    "\n".join(STRUCTURED_SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "structured_schema_migrations": (
        "migration_id", "schema_version", "schema_hash", "applied_at"
    ),
    "structured_run_contexts": (
        "run_id", "storage_namespace_id", "acquisition_scope", "ticker", "company_id",
        "dataset_registry_id", "dataset_registry_version", "dataset_registry_hash",
        "field_registry_id", "field_registry_version", "field_registry_hash",
        "requirement_set_id", "requirement_set_version", "requirement_set_hash",
        "query_pack_id", "query_pack_version", "query_pack_hash", "source_registry_id",
        "source_registry_version", "source_registry_hash", "peer_set_id", "peer_set_version",
        "peer_set_hash", "schedule_id", "schedule_version", "schedule_hash",
        "policy_version", "context_hash", "created_at", "payload",
    ),
    "structured_jobs": (
        "job_id", "run_id", "plan_item_id", "storage_namespace_id", "company_id",
        "ticker", "dataset_id", "source_definition_id", "source_definition_version",
        "purpose", "schedule_mode", "scope_key", "dedupe_key", "time_start", "time_end",
        "max_attempts", "ordinal", "created_at", "payload",
    ),
    "structured_pages": (
        "page_id", "job_id", "attempt_id", "snapshot_id", "page_number", "cursor",
        "position_key", "row_count", "total_count", "terminal", "content_hash",
        "lease_epoch", "committed_at", "payload",
    ),
    "structured_records": (
        "record_version_id", "job_id", "page_id", "snapshot_id", "row_key",
        "version_hash", "effective_at", "available_at", "observed_at",
        "supersedes_record_version_id", "payload",
    ),
    "structured_record_fields": (
        "field_value_id", "record_version_id", "dataset_id", "raw_field_name",
        "field_path", "standard_field_id", "definition_version", "nature", "quality",
        "value_json", "unit", "period_key", "payload",
    ),
    "structured_acquisition_coverage": (
        "coverage_record_id", "run_id", "shared_coverage_entry_id", "job_id", "company_id",
        "dataset_id", "scope_key", "status", "version", "safe_through", "available_from",
        "available_to", "reason_code", "supersedes_coverage_record_id", "recorded_at", "payload",
    ),
    "structured_reading_tasks": (
        "reading_task_id", "run_id", "company_id", "material_id", "content_version",
        "report_period", "state", "task_hash", "created_at", "payload",
    ),
    "structured_research_coverage_snapshots": (
        "coverage_snapshot_id", "run_id", "storage_namespace_id", "company_id", "ticker",
        "requirement_set_id", "requirement_set_version", "requirement_set_hash",
        "field_registry_version", "industry_profile_id", "industry_profile_version",
        "industry_profile_hash", "peer_set_id", "peer_set_version", "method_version",
        "as_of", "analysis_scope_json", "snapshot_hash", "evaluated_at", "payload",
    ),
    "structured_requirement_evaluations": (
        "evaluation_id", "coverage_snapshot_id", "company_id", "step_id", "question_id",
        "requirement_id", "period_key", "applicability", "readiness", "required_group_id",
        "optional", "reason_code", "evaluated_at", "payload",
    ),
}


_REQUIRED_INDEXES = {
    "idx_structured_context_company",
    "idx_structured_jobs_run",
    "idx_structured_jobs_scope",
    "idx_structured_pages_job",
    "idx_structured_records_row",
    "idx_structured_records_snapshot",
    "idx_structured_fields_standard",
    "idx_structured_fields_raw",
    "idx_structured_coverage_scope",
    "idx_structured_reading_state",
    "idx_structured_research_coverage_company",
    "idx_structured_evaluations_question",
}


def ensure_structured_storage(
    db_path: Path | str,
    data_root: Path | str,
    storage_namespace_id: str,
    *,
    busy_timeout_ms: int = 5_000,
    lock_timeout_seconds: float = 5.0,
    fault_injector: Callable[[str], None] | None = None,
) -> ModuleMigrationResult:
    """Apply the independent module migration without changing PRAGMA user_version."""

    path = Path(db_path)
    root = Path(data_root)
    identity = hashlib.sha256(
        os.path.normcase(str(path.resolve(strict=False))).encode("utf-8")
    ).hexdigest()
    with InterProcessFileLock(
        f"structured-module-{identity}", timeout_seconds=lock_timeout_seconds
    ):
        preflight = inspect_database(path)
        validate_preflight(preflight)
        if preflight.version != LATEST_SCHEMA_VERSION:
            raise StructuredSchemaError(
                f"structured module requires global schema v{LATEST_SCHEMA_VERSION}"
            )
        connection = _open(path, busy_timeout_ms)
        try:
            _assert_namespace(connection, storage_namespace_id)
            record = _migration_record(connection)
            if record is not None:
                _validate_module_schema(connection)
                return ModuleMigrationResult(
                    MODULE_MIGRATION_ID,
                    MODULE_SCHEMA_VERSION,
                    False,
                    None,
                    preflight.version,
                )
            dirty = _existing_module_objects(connection)
            if dirty:
                raise StructuredSchemaError(
                    "structured module objects exist without a migration record: "
                    + ", ".join(sorted(dirty))
                )
        finally:
            connection.close()

        backup = create_verified_backup(path, root, label="pre-structured-data-0001")
        connection = _open(path, busy_timeout_ms)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _assert_namespace(connection, storage_namespace_id)
            if _migration_record(connection) is not None:
                # Another initializer cannot normally pass the process lock, but
                # the transaction recheck keeps this safe for alternate callers.
                _validate_module_schema(connection)
                connection.execute("COMMIT")
                return ModuleMigrationResult(
                    MODULE_MIGRATION_ID,
                    MODULE_SCHEMA_VERSION,
                    False,
                    None,
                    preflight.version,
                )
            for ordinal, statement in enumerate(STRUCTURED_SCHEMA_STATEMENTS, start=1):
                connection.execute(statement)
                if fault_injector is not None:
                    fault_injector(f"{MODULE_MIGRATION_ID}:{ordinal}")
            connection.execute(
                "INSERT INTO structured_schema_migrations("
                "migration_id,schema_version,schema_hash,applied_at) VALUES (?,?,?,?)",
                (
                    MODULE_MIGRATION_ID,
                    MODULE_SCHEMA_VERSION,
                    MODULE_SCHEMA_HASH,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            _validate_module_schema(connection)
            current_user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_user_version != preflight.version:
                raise StructuredSchemaError("module migration changed global user_version")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return ModuleMigrationResult(
            MODULE_MIGRATION_ID,
            MODULE_SCHEMA_VERSION,
            True,
            backup,
            preflight.version,
        )


def validate_structured_schema(
    db_path: Path | str,
    storage_namespace_id: str,
    *,
    busy_timeout_ms: int = 5_000,
) -> None:
    connection = _open(Path(db_path), busy_timeout_ms, readonly=True)
    try:
        _assert_namespace(connection, storage_namespace_id)
        _validate_module_schema(connection)
    finally:
        connection.close()


class StructuredStorage:
    """Append-only structured projections bound to the shared v6 control plane."""

    def __init__(
        self,
        db_path: Path | str,
        storage_namespace_id: str,
        *,
        data_root: Path | str | None = None,
        initialize: bool = False,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.db_path = Path(db_path)
        self.storage_namespace_id = str(storage_namespace_id)
        self.busy_timeout_ms = int(busy_timeout_ms)
        if initialize:
            if data_root is None:
                raise ValueError("structured initialization requires data_root for backup")
            ensure_structured_storage(
                self.db_path,
                data_root,
                self.storage_namespace_id,
                busy_timeout_ms=self.busy_timeout_ms,
            )
        validate_structured_schema(
            self.db_path,
            self.storage_namespace_id,
            busy_timeout_ms=self.busy_timeout_ms,
        )

    @contextmanager
    def _connect(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        connection = _open(
            self.db_path, self.busy_timeout_ms, readonly=readonly
        )
        try:
            _assert_namespace(connection, self.storage_namespace_id)
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def persist_plan_bundle(
        self,
        repository: Any,
        *,
        run: Any,
        context: StructuredRunContext | Mapping[str, Any],
        plan_items: Iterable[Any],
        coverage_entries: Iterable[Any],
        links: Iterable[Any],
        jobs: Iterable[Mapping[str, Any]],
    ) -> None:
        """Atomically save the shared plan graph and its structured projections."""

        frozen = _coerce_context(context)
        item_values = tuple(plan_items)
        coverage_values = tuple(coverage_entries)
        link_values = tuple(links)
        job_values = tuple(_mapping(item) for item in jobs)

        def extension_writer(connection: sqlite3.Connection) -> None:
            self._save_context(connection, run, frozen)
            for job in job_values:
                self._save_job(connection, frozen, job)

        repository.save_plan_bundle(
            run,
            item_values,
            coverage_values,
            link_values,
            extension_writer=extension_writer,
        )

    def _save_context(
        self,
        connection: sqlite3.Connection,
        run: Any,
        context: StructuredRunContext,
    ) -> None:
        run_data = _mapping(run)
        expected = {
            "run_id": context.run_id,
            "ticker": context.ticker,
            "storage_namespace_id": context.storage_namespace_id,
            "registry_id": context.source_registry_id,
            "registry_version": context.source_registry_version,
        }
        for name, value in expected.items():
            if str(run_data.get(name) or "") != str(value):
                raise StructuredStorageError(
                    f"structured context {name} does not match shared run"
                )
        run_registry_hash = run_data.get("registry_content_hash") or run_data.get(
            "registry_hash"
        )
        if run_registry_hash != context.source_registry_hash:
            raise StructuredStorageError(
                "structured context source registry hash does not match shared run"
            )
        if context.storage_namespace_id != self.storage_namespace_id:
            raise StructuredNamespaceMismatch("structured context uses another namespace")
        data = asdict(context)
        payload = canonical_json(data)
        _insert_immutable(
            connection,
            "structured_run_contexts",
            "run_id=?",
            (context.run_id,),
            (
                "run_id", "storage_namespace_id", "acquisition_scope", "ticker", "company_id",
                "dataset_registry_id", "dataset_registry_version", "dataset_registry_hash",
                "field_registry_id", "field_registry_version", "field_registry_hash",
                "requirement_set_id", "requirement_set_version", "requirement_set_hash",
                "query_pack_id", "query_pack_version", "query_pack_hash",
                "source_registry_id", "source_registry_version", "source_registry_hash",
                "peer_set_id", "peer_set_version", "peer_set_hash", "schedule_id",
                "schedule_version", "schedule_hash", "policy_version", "context_hash",
                "created_at", "payload",
            ),
            (
                context.run_id, context.storage_namespace_id, context.acquisition_scope,
                context.ticker, context.company_id, context.dataset_registry_id,
                context.dataset_registry_version, context.dataset_registry_hash,
                context.field_registry_id, context.field_registry_version,
                context.field_registry_hash, context.requirement_set_id,
                context.requirement_set_version, context.requirement_set_hash,
                context.query_pack_id, context.query_pack_version, context.query_pack_hash,
                context.source_registry_id, context.source_registry_version,
                context.source_registry_hash, context.peer_set_id, context.peer_set_version,
                context.peer_set_hash, context.schedule_id, context.schedule_version,
                context.schedule_hash, context.policy_version, context.content_hash,
                context.created_at.isoformat(), payload,
            ),
            payload,
        )

    def _save_job(
        self,
        connection: sqlite3.Connection,
        context: StructuredRunContext,
        value: Mapping[str, Any],
    ) -> None:
        job_id = _required(value, "job_id")
        if _required(value, "run_id") != context.run_id:
            raise StructuredStorageError("structured job references another run")
        if _required(value, "company_id") != context.company_id:
            raise StructuredStorageError("structured job references another company")
        if _required(value, "ticker") != context.ticker:
            raise StructuredStorageError("structured job references another ticker")
        if str(value.get("storage_namespace_id") or context.storage_namespace_id) != self.storage_namespace_id:
            raise StructuredNamespaceMismatch("structured job references another namespace")
        plan = connection.execute(
            "SELECT run_id,source_definition_id,source_definition_version FROM "
            "physical_query_plan_items WHERE plan_item_id=?",
            (_required(value, "plan_item_id"),),
        ).fetchone()
        if plan is None or plan["run_id"] != context.run_id:
            raise StructuredStorageError("structured job plan item is not in its shared run")
        for name in ("source_definition_id", "source_definition_version"):
            if str(plan[name]) != str(_required(value, name)):
                raise StructuredStorageError(
                    f"structured job {name} does not match shared physical plan"
                )
        data = dict(value)
        data.setdefault("storage_namespace_id", self.storage_namespace_id)
        data.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        payload = canonical_json(data)
        _insert_immutable(
            connection,
            "structured_jobs",
            "job_id=?",
            (job_id,),
            (
                "job_id", "run_id", "plan_item_id", "storage_namespace_id", "company_id",
                "ticker", "dataset_id", "source_definition_id", "source_definition_version",
                "purpose", "schedule_mode", "scope_key", "dedupe_key", "time_start",
                "time_end", "max_attempts", "ordinal", "created_at", "payload",
            ),
            (
                job_id, context.run_id, _required(data, "plan_item_id"),
                self.storage_namespace_id, context.company_id, context.ticker,
                _required(data, "dataset_id"), _required(data, "source_definition_id"),
                _required(data, "source_definition_version"), _required(data, "purpose"),
                _required(data, "schedule_mode"), _required(data, "scope_key"),
                _required(data, "dedupe_key"), _optional_iso(data.get("time_start")),
                _optional_iso(data.get("time_end")), int(data.get("max_attempts", 2)),
                int(data.get("ordinal", 0)), _iso(data["created_at"]), payload,
            ),
            payload,
        )

    def get_run_context(
        self,
        run_id: str,
        *,
        expected_pins: Mapping[str, Any] | None = None,
    ) -> StructuredRunContext:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT payload,context_hash FROM structured_run_contexts WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise AcquisitionNotFoundError(f"structured run context not found: {run_id}")
        context = StructuredRunContext.from_mapping(json.loads(row["payload"]))
        if context.content_hash != row["context_hash"]:
            raise StructuredSchemaError("structured run context hash mismatch")
        for name, expected in (expected_pins or {}).items():
            if not hasattr(context, name) or getattr(context, name) != expected:
                raise StructuredStorageError(
                    f"frozen structured context pin mismatch: {name}"
                )
        return context

    def list_jobs(
        self,
        run_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM structured_jobs WHERE run_id=? "
                "ORDER BY ordinal,job_id LIMIT ? OFFSET ?",
                (run_id, -1 if limit is None else int(limit), int(offset)),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT payload FROM structured_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise AcquisitionNotFoundError(f"structured job not found: {job_id}")
        return json.loads(row["payload"])

    def find_run_for_dedupe_key(self, dedupe_key: str) -> str | None:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT run_id FROM structured_jobs WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
        return None if row is None else str(row["run_id"])

    def commit_page_bundle(
        self,
        repository: Any,
        *,
        page: Mapping[str, Any],
        records: Iterable[Mapping[str, Any]] = (),
        fields: Iterable[Mapping[str, Any]] = (),
        owner_token: str,
        lease_epoch: int,
    ) -> None:
        page_data = dict(page)
        record_values = tuple(dict(item) for item in records)
        field_values = tuple(dict(item) for item in fields)
        job = self.get_job(_required(page_data, "job_id"))
        run_id = _required(job, "run_id")

        def writer(connection: sqlite3.Connection) -> None:
            self._save_page(connection, job, page_data, lease_epoch)
            for record in record_values:
                self._save_record(connection, page_data, record)
            record_ids = {_required(item, "record_version_id") for item in record_values}
            for item in field_values:
                record_id = _required(item, "record_version_id")
                if record_id not in record_ids and connection.execute(
                    "SELECT 1 FROM structured_records WHERE record_version_id=?",
                    (record_id,),
                ).fetchone() is None:
                    raise StructuredStorageError("structured field has no record version")
                self._save_field(connection, item)

        repository.commit_extension_with_lease(
            run_id,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
            writer=writer,
        )

    def _save_page(
        self,
        connection: sqlite3.Connection,
        job: Mapping[str, Any],
        value: Mapping[str, Any],
        lease_epoch: int,
    ) -> None:
        if int(_required(value, "lease_epoch")) != int(lease_epoch):
            raise StructuredStorageError("structured page lease epoch mismatch")
        attempt = connection.execute(
            "SELECT run_id,physical_query_plan_item_id,lease_epoch FROM acquisition_attempts "
            "WHERE attempt_id=?",
            (_required(value, "attempt_id"),),
        ).fetchone()
        snapshot = connection.execute(
            "SELECT storage_namespace_id,physical_query_plan_item_id,"
            "source_definition_id,source_definition_version,content_sha256,byte_length,"
            "query_page_canonical FROM "
            "raw_resource_snapshots WHERE snapshot_id=?",
            (_required(value, "snapshot_id"),),
        ).fetchone()
        if attempt is None or snapshot is None:
            raise StructuredStorageError("structured page requires shared attempt and snapshot")
        if (
            attempt["run_id"] != job["run_id"]
            or attempt["physical_query_plan_item_id"] != job["plan_item_id"]
            or int(attempt["lease_epoch"]) != int(lease_epoch)
        ):
            raise StructuredStorageError("structured page attempt lineage mismatch")
        if snapshot["storage_namespace_id"] != self.storage_namespace_id:
            raise StructuredNamespaceMismatch("structured page snapshot lineage mismatch")
        direct_plan = snapshot["physical_query_plan_item_id"] == job["plan_item_id"]
        observed_plan = connection.execute(
            "SELECT o.payload FROM discovery_observations o "
            "JOIN acquisition_attempts a ON a.attempt_id=o.attempt_id "
            "WHERE o.snapshot_id=? AND o.physical_query_plan_item_id=? "
            "AND a.run_id=? AND a.physical_query_plan_item_id=? "
            "AND a.source_definition_id=? AND a.source_definition_version=? "
            "AND a.attempt_kind='discovery' LIMIT 1",
            (
                _required(value, "snapshot_id"),
                job["plan_item_id"],
                job["run_id"],
                job["plan_item_id"],
                job["source_definition_id"],
                job["source_definition_version"],
            ),
        ).fetchone()
        observed_payload = (
            None if observed_plan is None else json.loads(observed_plan["payload"])
        )
        observation_valid = observed_payload is not None and _observation_matches_snapshot(
            observed_payload, snapshot
        )
        if (
            str(snapshot["source_definition_id"])
            != str(job["source_definition_id"])
            or str(snapshot["source_definition_version"])
            != str(job["source_definition_version"])
            or (not direct_plan and not observation_valid)
        ):
            raise StructuredNamespaceMismatch("structured page snapshot lineage mismatch")
        data = dict(value)
        data.setdefault("committed_at", datetime.now(timezone.utc).isoformat())
        payload = canonical_json(data)
        _insert_immutable(
            connection,
            "structured_pages",
            "page_id=?",
            (_required(data, "page_id"),),
            (
                "page_id", "job_id", "attempt_id", "snapshot_id", "page_number", "cursor",
                "position_key", "row_count", "total_count", "terminal", "content_hash",
                "lease_epoch", "committed_at", "payload",
            ),
            (
                _required(data, "page_id"), _required(data, "job_id"),
                _required(data, "attempt_id"), _required(data, "snapshot_id"),
                data.get("page_number"), data.get("cursor"), _required(data, "position_key"),
                int(data.get("row_count", 0)), data.get("total_count"),
                1 if bool(data.get("terminal")) else 0,
                _require_sha256(_required(data, "content_hash"), "content_hash"),
                int(lease_epoch), _iso(data["committed_at"]), payload,
            ),
            payload,
        )

    def _save_record(
        self,
        connection: sqlite3.Connection,
        page: Mapping[str, Any],
        value: Mapping[str, Any],
    ) -> None:
        if _required(value, "job_id") != _required(page, "job_id"):
            raise StructuredStorageError("structured record references another job")
        if _required(value, "page_id") != _required(page, "page_id"):
            raise StructuredStorageError("structured record references another page")
        if _required(value, "snapshot_id") != _required(page, "snapshot_id"):
            raise StructuredStorageError("structured record references another snapshot")
        data = dict(value)
        payload = canonical_json(data)
        _insert_immutable(
            connection,
            "structured_records",
            "record_version_id=?",
            (_required(data, "record_version_id"),),
            (
                "record_version_id", "job_id", "page_id", "snapshot_id", "row_key",
                "version_hash", "effective_at", "available_at", "observed_at",
                "supersedes_record_version_id", "payload",
            ),
            (
                _required(data, "record_version_id"), _required(data, "job_id"),
                _required(data, "page_id"), _required(data, "snapshot_id"),
                _required(data, "row_key"),
                _require_sha256(_required(data, "version_hash"), "version_hash"),
                _optional_iso(data.get("effective_at")), _iso(_required(data, "available_at")),
                _iso(_required(data, "observed_at")), data.get("supersedes_record_version_id"),
                payload,
            ),
            payload,
        )

    @staticmethod
    def _save_field(connection: sqlite3.Connection, value: Mapping[str, Any]) -> None:
        data = dict(value)
        payload = canonical_json(data)
        raw_value = data.get("value") if "value" in data else data.get("value_json")
        _insert_immutable(
            connection,
            "structured_record_fields",
            "field_value_id=?",
            (_required(data, "field_value_id"),),
            (
                "field_value_id", "record_version_id", "dataset_id", "raw_field_name",
                "field_path", "standard_field_id", "definition_version", "nature", "quality",
                "value_json", "unit", "period_key", "payload",
            ),
            (
                _required(data, "field_value_id"), _required(data, "record_version_id"),
                _required(data, "dataset_id"), _required(data, "raw_field_name"),
                _required(data, "field_path"), data.get("standard_field_id"),
                data.get("definition_version"), _required(data, "nature"),
                _required(data, "quality"), canonical_json(raw_value) if raw_value is not None else None,
                data.get("unit"), data.get("period_key"), payload,
            ),
            payload,
        )

    def list_pages(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM structured_pages WHERE job_id=? "
                "ORDER BY committed_at,page_id", (job_id,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_records(
        self,
        *,
        job_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = "WHERE job_id=?" if job_id is not None else ""
        args: tuple[Any, ...] = () if job_id is None else (job_id,)
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM structured_records {where} "
                "ORDER BY job_id,row_key,available_at,record_version_id LIMIT ? OFFSET ?",
                (*args, -1 if limit is None else int(limit), int(offset)),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_record_fields(
        self,
        *,
        record_version_id: str | None = None,
        standard_field_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        args: list[Any] = []
        if record_version_id is not None:
            filters.append("record_version_id=?")
            args.append(record_version_id)
        if standard_field_id is not None:
            filters.append("standard_field_id=?")
            args.append(standard_field_id)
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM structured_record_fields {where} "
                "ORDER BY record_version_id,field_path,field_value_id LIMIT ? OFFSET ?",
                (*args, -1 if limit is None else int(limit), int(offset)),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def unprojected_snapshot_ids(self, job_id: str) -> tuple[str, ...]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT s.snapshot_id,s.created_at FROM structured_jobs j "
                "JOIN raw_resource_snapshots s ON "
                "(s.physical_query_plan_item_id=j.plan_item_id OR EXISTS ("
                "SELECT 1 FROM discovery_observations o "
                "JOIN acquisition_attempts a ON a.attempt_id=o.attempt_id "
                "WHERE o.snapshot_id=s.snapshot_id "
                "AND o.physical_query_plan_item_id=j.plan_item_id "
                "AND a.run_id=j.run_id "
                "AND a.physical_query_plan_item_id=j.plan_item_id "
                "AND a.source_definition_id=j.source_definition_id "
                "AND a.source_definition_version=j.source_definition_version "
                "AND a.attempt_kind='discovery')) "
                "LEFT JOIN structured_pages p ON p.snapshot_id=s.snapshot_id AND p.job_id=j.job_id "
                "WHERE j.job_id=? AND p.page_id IS NULL "
                "AND s.storage_namespace_id=j.storage_namespace_id "
                "AND s.source_definition_id=j.source_definition_id "
                "AND s.source_definition_version=j.source_definition_version "
                "ORDER BY s.created_at,s.snapshot_id",
                (job_id,),
            ).fetchall()
        return tuple(str(row["snapshot_id"]) for row in rows)

    def snapshot_observation_id(self, job_id: str, snapshot_id: str) -> str:
        """Return the observation proving this job plan saw the snapshot."""

        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT o.observation_id,o.payload,s.content_sha256,s.byte_length,"
                "s.query_page_canonical FROM structured_jobs j "
                "JOIN discovery_observations o "
                "ON o.physical_query_plan_item_id=j.plan_item_id "
                "JOIN acquisition_attempts a ON a.attempt_id=o.attempt_id "
                "JOIN raw_resource_snapshots s ON s.snapshot_id=o.snapshot_id "
                "WHERE j.job_id=? AND o.snapshot_id=? "
                "AND a.run_id=j.run_id "
                "AND a.physical_query_plan_item_id=j.plan_item_id "
                "AND a.source_definition_id=j.source_definition_id "
                "AND a.source_definition_version=j.source_definition_version "
                "AND a.attempt_kind='discovery' "
                "AND s.storage_namespace_id=j.storage_namespace_id "
                "AND s.source_definition_id=j.source_definition_id "
                "AND s.source_definition_version=j.source_definition_version "
                "ORDER BY o.observed_at DESC,o.observation_id DESC LIMIT 1",
                (job_id, snapshot_id),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if _observation_matches_snapshot(payload, row):
                return str(row["observation_id"])
        if not rows:
            raise StructuredStorageError(
                "structured snapshot has no observation for the job plan"
            )
        raise StructuredStorageError(
            "structured snapshot observation does not match the saved response"
        )

    def append_acquisition_coverage(self, value: Mapping[str, Any]) -> None:
        data = dict(value)
        if data.get("storage_namespace_id", self.storage_namespace_id) != self.storage_namespace_id:
            raise StructuredNamespaceMismatch("coverage references another namespace")
        data.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
        payload = canonical_json(data)
        with self._write() as connection:
            _insert_immutable(
                connection,
                "structured_acquisition_coverage",
                "coverage_record_id=?",
                (_required(data, "coverage_record_id"),),
                (
                    "coverage_record_id", "run_id", "shared_coverage_entry_id", "job_id",
                    "company_id", "dataset_id", "scope_key", "status", "version",
                    "safe_through", "available_from", "available_to", "reason_code",
                    "supersedes_coverage_record_id", "recorded_at", "payload",
                ),
                (
                    _required(data, "coverage_record_id"), _required(data, "run_id"),
                    data.get("shared_coverage_entry_id"), data.get("job_id"),
                    _required(data, "company_id"), _required(data, "dataset_id"),
                    _required(data, "scope_key"), _required(data, "status"),
                    int(data.get("version", 1)), data.get("safe_through"),
                    data.get("available_from"), data.get("available_to"), data.get("reason_code"),
                    data.get("supersedes_coverage_record_id"), _iso(data["recorded_at"]), payload,
                ),
                payload,
            )

    def list_acquisition_coverage(
        self,
        *,
        run_id: str | None = None,
        company_id: str | None = None,
        dataset_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        args: list[Any] = []
        for column, value in (
            ("run_id", run_id),
            ("company_id", company_id),
            ("dataset_id", dataset_id),
            ("status", status),
        ):
            if value is not None:
                filters.append(f"{column}=?")
                args.append(value)
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM structured_acquisition_coverage {where} "
                "ORDER BY run_id,company_id,dataset_id,scope_key,version,coverage_record_id "
                "LIMIT ? OFFSET ?",
                (*args, -1 if limit is None else int(limit), int(offset)),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def append_reading_task(self, value: Mapping[str, Any]) -> str:
        data = dict(value)
        run_id = _required(data, "run_id")
        context = self.get_run_context(run_id)
        if _required(data, "company_id") != context.company_id:
            raise StructuredStorageError("reading task references another company")
        data.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        identity = {
            "run_id": run_id,
            "company_id": context.company_id,
            "material_id": _required(data, "material_id"),
            "content_version": _required(data, "content_version"),
            "report_period": data.get("report_period"),
            "task_key": data.get("task_key"),
            "rule_ids": data.get("rule_ids", ()),
            "target_sections": data.get("target_sections", ()),
        }
        data.setdefault("task_hash", canonical_sha256(identity))
        data.setdefault(
            "reading_task_id",
            stable_structured_id(
                "reading-task", {"run_id": run_id, "task_hash": data["task_hash"]}
            ),
        )
        task_id = _required(data, "reading_task_id")
        payload = canonical_json(data)
        with self._write() as connection:
            _insert_immutable(
                connection,
                "structured_reading_tasks",
                "reading_task_id=?",
                (task_id,),
                (
                    "reading_task_id", "run_id", "company_id", "material_id",
                    "content_version", "report_period", "state", "task_hash",
                    "created_at", "payload",
                ),
                (
                    task_id, run_id, context.company_id, _required(data, "material_id"),
                    _required(data, "content_version"), data.get("report_period"),
                    _required(data, "state"),
                    _require_sha256(_required(data, "task_hash"), "task_hash"),
                    _iso(data["created_at"]), payload,
                ),
                payload,
            )
        return task_id

    def get_reading_task(self, reading_task_id: str) -> dict[str, Any]:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT payload FROM structured_reading_tasks WHERE reading_task_id=?",
                (reading_task_id,),
            ).fetchone()
        if row is None:
            raise AcquisitionNotFoundError(
                f"structured reading task not found: {reading_task_id}"
            )
        return json.loads(row["payload"])

    def list_reading_tasks(
        self,
        *,
        run_id: str,
        state: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters = ["run_id=?"]
        args: list[Any] = [run_id]
        if state is not None:
            filters.append("state=?")
            args.append(state)
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM structured_reading_tasks WHERE "
                + " AND ".join(filters)
                + " ORDER BY created_at,reading_task_id LIMIT ? OFFSET ?",
                (*args, -1 if limit is None else int(limit), int(offset)),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_research_coverage_snapshot(
        self,
        snapshot: Mapping[str, Any],
        evaluations: Iterable[Mapping[str, Any]],
    ) -> str:
        """Freeze one coverage result and all requirement rows in one transaction."""

        data = dict(snapshot)
        evaluation_values = tuple(dict(item) for item in evaluations)
        run_id = _required(data, "run_id")
        context = self.get_run_context(run_id)
        if _required(data, "company_id") != context.company_id:
            raise StructuredStorageError("coverage snapshot references another company")
        if _required(data, "ticker") != context.ticker:
            raise StructuredStorageError("coverage snapshot references another ticker")
        if _required(data, "requirement_set_id") != context.requirement_set_id:
            raise StructuredStorageError("coverage requirement-set id does not match context")
        if _required(data, "requirement_set_version") != context.requirement_set_version:
            raise StructuredStorageError("coverage requirement-set version does not match context")
        if _required(data, "requirement_set_hash") != context.requirement_set_hash:
            raise StructuredStorageError("coverage requirement-set hash does not match context")
        if _required(data, "field_registry_version") != context.field_registry_version:
            raise StructuredStorageError("coverage field-registry version does not match context")
        data.setdefault("storage_namespace_id", self.storage_namespace_id)
        if data["storage_namespace_id"] != self.storage_namespace_id:
            raise StructuredNamespaceMismatch("coverage snapshot references another namespace")
        data.setdefault("evaluated_at", datetime.now(timezone.utc).isoformat())
        data["analysis_scope"] = _json_round_trip(data.get("analysis_scope", {}))
        snapshot_hash = canonical_sha256(
            {key: value for key, value in data.items() if key != "snapshot_hash"}
        )
        if data.get("snapshot_hash") not in (None, snapshot_hash):
            raise StructuredStorageError("coverage snapshot hash is not canonical")
        data["snapshot_hash"] = snapshot_hash
        payload = canonical_json(data)
        snapshot_id = _required(data, "coverage_snapshot_id")
        with self._write() as connection:
            _insert_immutable(
                connection,
                "structured_research_coverage_snapshots",
                "coverage_snapshot_id=?",
                (snapshot_id,),
                (
                    "coverage_snapshot_id", "run_id", "storage_namespace_id", "company_id",
                    "ticker", "requirement_set_id", "requirement_set_version",
                    "requirement_set_hash", "field_registry_version", "industry_profile_id",
                    "industry_profile_version", "industry_profile_hash", "peer_set_id",
                    "peer_set_version", "method_version", "as_of", "analysis_scope_json",
                    "snapshot_hash", "evaluated_at", "payload",
                ),
                (
                    snapshot_id, run_id, self.storage_namespace_id, context.company_id,
                    context.ticker, context.requirement_set_id,
                    _required(data, "requirement_set_version"),
                    _require_sha256(_required(data, "requirement_set_hash"), "requirement_set_hash"),
                    _required(data, "field_registry_version"),
                    _required(data, "industry_profile_id"),
                    _required(data, "industry_profile_version"),
                    _require_sha256(_required(data, "industry_profile_hash"), "industry_profile_hash"),
                    data.get("peer_set_id"), data.get("peer_set_version"),
                    data.get("method_version"), _iso(_required(data, "as_of")),
                    canonical_json(data["analysis_scope"]), snapshot_hash,
                    _iso(data["evaluated_at"]), payload,
                ),
                payload,
            )
            for evaluation in evaluation_values:
                self._save_evaluation(connection, snapshot_id, context.company_id, evaluation)
        return snapshot_hash

    @staticmethod
    def _save_evaluation(
        connection: sqlite3.Connection,
        snapshot_id: str,
        company_id: str,
        value: Mapping[str, Any],
    ) -> None:
        data = dict(value)
        if _required(data, "coverage_snapshot_id") != snapshot_id:
            raise StructuredStorageError("requirement evaluation references another snapshot")
        if _required(data, "company_id") != company_id:
            raise StructuredStorageError("requirement evaluation references another company")
        data.setdefault("evaluated_at", datetime.now(timezone.utc).isoformat())
        payload = canonical_json(data)
        _insert_immutable(
            connection,
            "structured_requirement_evaluations",
            "evaluation_id=?",
            (_required(data, "evaluation_id"),),
            (
                "evaluation_id", "coverage_snapshot_id", "company_id", "step_id",
                "question_id", "requirement_id", "period_key", "applicability",
                "readiness", "required_group_id", "optional", "reason_code",
                "evaluated_at", "payload",
            ),
            (
                _required(data, "evaluation_id"), snapshot_id, company_id,
                _required(data, "step_id"), _required(data, "question_id"),
                _required(data, "requirement_id"), _required(data, "period_key"),
                _required(data, "applicability"), _required(data, "readiness"),
                data.get("required_group_id"), 1 if bool(data.get("optional")) else 0,
                data.get("reason_code"), _iso(data["evaluated_at"]), payload,
            ),
            payload,
        )

    def get_research_coverage_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT payload,snapshot_hash FROM structured_research_coverage_snapshots "
                "WHERE coverage_snapshot_id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise AcquisitionNotFoundError(f"research coverage snapshot not found: {snapshot_id}")
        data = json.loads(row["payload"])
        expected = canonical_sha256(
            {key: value for key, value in data.items() if key != "snapshot_hash"}
        )
        if row["snapshot_hash"] != expected or data.get("snapshot_hash") != expected:
            raise StructuredSchemaError("research coverage snapshot hash mismatch")
        return data

    def list_requirement_evaluations(
        self,
        snapshot_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM structured_requirement_evaluations "
                "WHERE coverage_snapshot_id=? ORDER BY step_id,question_id,requirement_id,period_key "
                "LIMIT ? OFFSET ?",
                (snapshot_id, -1 if limit is None else int(limit), int(offset)),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]


def _open(
    path: Path,
    busy_timeout_ms: int,
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    if readonly:
        from urllib.parse import quote

        connection = sqlite3.connect(
            f"file:{quote(str(path.resolve()))}?mode=ro",
            uri=True,
            timeout=max(int(busy_timeout_ms), 1) / 1000,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(
            path,
            timeout=max(int(busy_timeout_ms), 1) / 1000,
            isolation_level=None,
        )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return connection


def _assert_namespace(connection: sqlite3.Connection, namespace_id: str) -> None:
    rows = connection.execute(
        "SELECT namespace_id FROM storage_namespaces ORDER BY namespace_id"
    ).fetchall()
    if len(rows) != 1 or rows[0]["namespace_id"] != namespace_id:
        raise StructuredNamespaceMismatch(
            "structured module namespace does not match the bound database"
        )


def _migration_record(connection: sqlite3.Connection) -> sqlite3.Row | None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='structured_schema_migrations'"
    ).fetchone()
    if table is None:
        return None
    row = connection.execute(
        "SELECT migration_id,schema_version,schema_hash FROM structured_schema_migrations "
        "WHERE migration_id=?", (MODULE_MIGRATION_ID,)
    ).fetchone()
    if row is not None and (
        int(row["schema_version"]) != MODULE_SCHEMA_VERSION
        or row["schema_hash"] != MODULE_SCHEMA_HASH
    ):
        raise StructuredSchemaError("structured module migration record is incompatible")
    return row


def _existing_module_objects(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE (type='table' OR type='index') "
        "AND name LIKE 'structured_%'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _validate_module_schema(connection: sqlite3.Connection) -> None:
    record = _migration_record(connection)
    if record is None:
        raise StructuredSchemaError("structured module migration record is missing")
    for table, expected_columns in _REQUIRED_COLUMNS.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        actual = tuple(str(row["name"]) for row in rows)
        if actual != expected_columns:
            raise StructuredSchemaError(
                f"structured module table columns mismatch: {table}"
            )
    indexes = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    missing_indexes = _REQUIRED_INDEXES - indexes
    if missing_indexes:
        raise StructuredSchemaError(
            "structured module indexes missing: " + ", ".join(sorted(missing_indexes))
        )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise StructuredSchemaError("structured module database has foreign-key violations")


def _insert_immutable(
    connection: sqlite3.Connection,
    table: str,
    key_where: str,
    key_values: Sequence[Any],
    columns: Sequence[str],
    values: Sequence[Any],
    payload: str,
) -> bool:
    existing = connection.execute(
        f"SELECT payload FROM {table} WHERE {key_where}", tuple(key_values)
    ).fetchone()
    if existing is not None:
        if existing["payload"] != payload:
            raise ImmutableRecordError(f"{table} record cannot be overwritten")
        return False
    try:
        connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values),
        )
    except sqlite3.IntegrityError as exc:
        if "unique" in str(exc).lower() or "primary key" in str(exc).lower():
            raise ImmutableRecordError(
                f"{table} immutable identity already exists"
            ) from exc
        raise AcquisitionStorageError(f"{table} relational constraint failed") from exc
    return True


def _coerce_context(
    value: StructuredRunContext | Mapping[str, Any],
) -> StructuredRunContext:
    return value if isinstance(value, StructuredRunContext) else StructuredRunContext.from_mapping(value)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _json_round_trip(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=False)
    if hasattr(value, "__dataclass_fields__"):
        return _json_round_trip(asdict(value))
    raise TypeError(f"unsupported structured storage value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_structured_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{canonical_sha256(value)[:24]}"


def _json_round_trip(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _observation_matches_snapshot(
    observation: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> bool:
    try:
        return (
            observation.get("response_sha256") == snapshot["content_sha256"]
            and int(observation.get("response_byte_length", -1))
            == int(snapshot["byte_length"])
            and bool(snapshot["query_page_canonical"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _required(value: Mapping[str, Any], name: str) -> str:
    found = value.get(name)
    if found is None or not str(found).strip():
        raise StructuredStorageError(f"structured value is missing {name}")
    return str(found)


def _require_sha256(value: str, name: str) -> str:
    if not _SHA256.fullmatch(str(value)):
        raise StructuredStorageError(f"{name} must be a lowercase sha256")
    return str(value)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("structured timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime):
        raise StructuredStorageError("structured timestamp is required")
    return _aware_utc(parsed).isoformat()


def _optional_iso(value: Any) -> str | None:
    return None if value is None else _iso(value)
