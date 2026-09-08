from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from analysis.acquisition.bootstrap import StorageBootstrapper
from analysis.acquisition.models import (
    AcquisitionAttempt,
    AcquisitionAttemptEvent,
    CoverageEntry,
    PhysicalQueryCoverageLink,
    PhysicalQueryPlanItem,
)
from analysis.acquisition.repository import (
    AcquisitionNotFoundError,
    AcquisitionRepository,
    StaleLeaseError,
)
from analysis.acquisition.snapshots import (
    ContentAddressedBlobStore,
    DiscoverySnapshotRequest,
    SnapshotService,
)
from analysis.acquisition.source_gate import CrossProcessSourceGate
from analysis.structured.scheduler import StructuredExecutionBridge
from analysis.structured.storage import (
    MODULE_MIGRATION_ID,
    MODULE_SCHEMA_HASH,
    MODULE_SCHEMA_VERSION,
    StructuredNamespaceMismatch,
    StructuredRunContext,
    StructuredSchemaError,
    StructuredStorage,
    StructuredStorageError,
    ensure_structured_storage,
)


def _context(store, *, run_id: str | None = None, ticker: str | None = None, frozen=None):
    return StructuredRunContext(
        run_id=run_id or store.run.run_id,
        storage_namespace_id=store.namespace.namespace_id,
        ticker=ticker or store.run.ticker,
        company_id=f"company:{ticker or store.run.ticker}",
        dataset_registry_id="structured-datasets",
        dataset_registry_version="1.0.0",
        dataset_registry_hash="a" * 64,
        field_registry_id="structured-fields",
        field_registry_version="1.0.0",
        field_registry_hash="b" * 64,
        requirement_set_id="eight-step-requirements",
        requirement_set_version="1.0.0",
        requirement_set_hash="c" * 64,
        query_pack_id="structured-query-pack",
        query_pack_version="1.0.0",
        query_pack_hash="d" * 64,
        source_registry_id=store.run.registry_id,
        source_registry_version=store.run.registry_version,
        source_registry_hash=store.run.registry_content_hash,
        peer_set_id="liquor-seven",
        peer_set_version="1.0.0",
        peer_set_hash="e" * 64,
        schedule_id="structured-schedule",
        schedule_version="1.0.0",
        schedule_hash="f" * 64,
        policy_version="structured-first-v1",
        frozen_config=frozen or {"dataset": {"page_size": 100}},
        created_at=store.now,
    )


def _job(store, context, *, job_id="job-1", plan_item=None, ordinal=0):
    plan = plan_item or store.plan_item
    return {
        "job_id": job_id,
        "run_id": context.run_id,
        "plan_item_id": plan.plan_item_id,
        "storage_namespace_id": context.storage_namespace_id,
        "company_id": context.company_id,
        "ticker": context.ticker,
        "dataset_id": f"dataset-{ordinal}",
        "source_definition_id": plan.source_definition_id,
        "source_definition_version": plan.source_definition_version,
        "purpose": "full_history",
        "schedule_mode": "all_available",
        "scope_key": f"scope-{ordinal}",
        "dedupe_key": hashlib.sha256(f"dedupe-{ordinal}".encode()).hexdigest(),
        "time_start": plan.time_start.isoformat(),
        "time_end": plan.time_end.isoformat(),
        "max_attempts": 2,
        "ordinal": ordinal,
        "created_at": store.now.isoformat(),
    }


def _initialize(store):
    result = ensure_structured_storage(
        store.db_path, store.data_root, store.namespace.namespace_id
    )
    return StructuredStorage(store.db_path, store.namespace.namespace_id), result


def _attach_existing_plan(store, storage, *, frozen=None):
    context = _context(store, frozen=frozen)
    job = _job(store, context)
    storage.persist_plan_bundle(
        store.repository,
        run=store.run,
        context=context,
        plan_items=(store.plan_item,),
        coverage_entries=store.coverage_entries,
        links=store.links,
        jobs=(job,),
    )
    return context, job


def test_module_migration_is_independent_idempotent_and_preserves_existing_v6(acquisition_store):
    store = acquisition_store
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "INSERT INTO sources(source_id,payload) VALUES (?,?)",
            ("sentinel-source", '{"sentinel":true}'),
        )
        before_migrations = tuple(
            row[0]
            for row in connection.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
            )
        )
        before_version = connection.execute("PRAGMA user_version").fetchone()[0]

    first = ensure_structured_storage(
        store.db_path, store.data_root, store.namespace.namespace_id
    )
    second = ensure_structured_storage(
        store.db_path, store.data_root, store.namespace.namespace_id
    )
    assert first.applied is True and first.backup is not None
    assert second.applied is False and second.backup is None

    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == before_version == 6
        assert tuple(
            row[0]
            for row in connection.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
            )
        ) == before_migrations
        assert connection.execute(
            "SELECT payload FROM sources WHERE source_id='sentinel-source'"
        ).fetchone()[0] == '{"sentinel":true}'
        assert connection.execute(
            "SELECT schema_version,schema_hash FROM structured_schema_migrations "
            "WHERE migration_id=?", (MODULE_MIGRATION_ID,)
        ).fetchone() == (MODULE_SCHEMA_VERSION, MODULE_SCHEMA_HASH)
        module_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'structured_%'"
            )
        }
        assert not any("peer" in name or "industry_fact" in name for name in module_tables)


def test_module_migration_rolls_back_midway_and_keeps_old_rows(tmp_path):
    db_path = tmp_path / "analysis.db"
    data_root = tmp_path / "data"
    bootstrap = StorageBootstrapper(db_path, data_root).bootstrap()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO sources(source_id,payload) VALUES ('sentinel','{}')"
        )

    def fail(stage: str) -> None:
        if stage == f"{MODULE_MIGRATION_ID}:4":
            raise RuntimeError("injected module migration failure")

    with pytest.raises(RuntimeError, match="injected"):
        ensure_structured_storage(
            db_path,
            data_root,
            bootstrap.namespace.namespace_id,
            fault_injector=fail,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT payload FROM sources WHERE source_id='sentinel'"
        ).fetchone()[0] == "{}"
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'structured_%'"
        ).fetchone()[0] == 0


def test_fake_module_record_and_wrong_namespace_fail_closed(tmp_path):
    db_path = tmp_path / "analysis.db"
    data_root = tmp_path / "data"
    bootstrap = StorageBootstrapper(db_path, data_root).bootstrap()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE structured_schema_migrations("
            "migration_id TEXT PRIMARY KEY,schema_version INTEGER NOT NULL,"
            "schema_hash TEXT NOT NULL,applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO structured_schema_migrations VALUES (?,?,?,?)",
            (MODULE_MIGRATION_ID, MODULE_SCHEMA_VERSION, MODULE_SCHEMA_HASH, datetime.now(timezone.utc).isoformat()),
        )
    with pytest.raises(StructuredSchemaError, match="columns mismatch"):
        ensure_structured_storage(
            db_path, data_root, bootstrap.namespace.namespace_id
        )
    with pytest.raises(StructuredNamespaceMismatch):
        ensure_structured_storage(db_path, data_root, "another-namespace")


def test_concurrent_module_initialization_applies_once(acquisition_store):
    store = acquisition_store

    def initialize():
        return ensure_structured_storage(
            store.db_path,
            store.data_root,
            store.namespace.namespace_id,
            lock_timeout_seconds=10,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: initialize(), range(2)))
    assert sorted(item.applied for item in results) == [False, True]
    assert sum(item.backup is not None for item in results) == 1


def test_context_and_shared_plan_are_atomic_and_resume_uses_frozen_config(acquisition_store):
    store = acquisition_store
    storage, _ = _initialize(store)
    mutable = {"dataset": {"page_size": 100}}
    context, job = _attach_existing_plan(store, storage, frozen=mutable)
    mutable["dataset"]["page_size"] = 999

    loaded = storage.get_run_context(store.run.run_id)
    assert loaded.frozen_config == {"dataset": {"page_size": 100}}
    assert storage.list_jobs(store.run.run_id, limit=None) == [job]
    with pytest.raises(Exception, match="pin mismatch"):
        storage.get_run_context(
            store.run.run_id,
            expected_pins={"field_registry_version": "2.0.0"},
        )

    run2 = store.run.model_copy(update={"run_id": "run-atomic-rollback"})
    plan2 = store.plan_item.model_copy(
        update={"run_id": run2.run_id, "plan_item_id": "plan-atomic-rollback"}
    )
    coverage2 = tuple(
        entry.model_copy(
            update={
                "run_id": run2.run_id,
                "coverage_entry_id": f"{entry.coverage_entry_id}-atomic",
            }
        )
        for entry in store.coverage_entries
    )
    links2 = tuple(
        PhysicalQueryCoverageLink(
            plan_item_id=plan2.plan_item_id,
            coverage_entry_id=entry.coverage_entry_id,
        )
        for entry in coverage2
    )
    bad_context = _context(store, run_id=run2.run_id, ticker="000001")
    bad_job = _job(store, bad_context, job_id="job-atomic", plan_item=plan2, ordinal=100)
    with pytest.raises(Exception, match="ticker does not match"):
        storage.persist_plan_bundle(
            store.repository,
            run=run2,
            context=bad_context,
            plan_items=(plan2,),
            coverage_entries=coverage2,
            links=links2,
            jobs=(bad_job,),
        )
    with pytest.raises(AcquisitionNotFoundError):
        store.repository.get_run(run2.run_id)
    assert storage.get_run_context(context.run_id) == loaded


def test_reading_and_acquisition_coverage_queries_are_complete_and_filterable(
    acquisition_store,
):
    store = acquisition_store
    storage, _ = _initialize(store)
    context, _ = _attach_existing_plan(store, storage)

    for index in range(501):
        storage.append_reading_task(
            {
                "reading_task_id": f"reading-{index:03d}",
                "run_id": context.run_id,
                "company_id": context.company_id,
                "material_id": f"material-{index:03d}",
                "content_version": hashlib.sha256(
                    f"material-{index}".encode()
                ).hexdigest(),
                "report_period": "2025Q4",
                "state": "failed" if index == 500 else "queued",
                "rule_ids": ["R01"],
                "created_at": (store.now + timedelta(microseconds=index)).isoformat(),
            }
        )
    reading = storage.list_reading_tasks(run_id=context.run_id, limit=None)
    assert len(reading) == 501
    assert reading[-1]["reading_task_id"] == "reading-500"
    assert storage.get_reading_task("reading-500")["state"] == "failed"
    assert [
        item["reading_task_id"]
        for item in storage.list_reading_tasks(
            run_id=context.run_id, state="failed", limit=None
        )
    ] == ["reading-500"]
    assert len(
        storage.list_reading_tasks(run_id=context.run_id, limit=100, offset=500)
    ) == 1
    with pytest.raises(StructuredStorageError, match="another company"):
        storage.append_reading_task(
            {
                "run_id": context.run_id,
                "company_id": "company:wrong",
                "material_id": "wrong",
                "content_version": "0" * 64,
                "state": "queued",
            }
        )

    for index in range(501):
        storage.append_acquisition_coverage(
            {
                "coverage_record_id": f"structured-coverage-{index:03d}",
                "run_id": context.run_id,
                "storage_namespace_id": context.storage_namespace_id,
                "company_id": context.company_id,
                "dataset_id": f"dataset-{index:03d}",
                "scope_key": f"scope-{index:03d}",
                "status": "failed" if index == 500 else "complete",
                "version": 1,
                "reason_code": "retry_exhausted" if index == 500 else None,
                "recorded_at": (store.now + timedelta(microseconds=index)).isoformat(),
            }
        )
    coverage = storage.list_acquisition_coverage(
        run_id=context.run_id, limit=None
    )
    assert len(coverage) == 501
    assert coverage[-1]["coverage_record_id"] == "structured-coverage-500"
    assert [
        item["coverage_record_id"]
        for item in storage.list_acquisition_coverage(
            run_id=context.run_id, status="failed", limit=None
        )
    ] == ["structured-coverage-500"]
    assert len(
        storage.list_acquisition_coverage(
            run_id=context.run_id, limit=100, offset=500
        )
    ) == 1


def test_missing_context_fails_before_source_gate_or_snapshot_use(acquisition_store):
    store = acquisition_store
    storage, _ = _initialize(store)
    bridge = StructuredExecutionBridge(
        repository=store.repository,
        storage=storage,
        source_gate=CrossProcessSourceGate(store.data_root),
        snapshot_service=SnapshotService(
            ContentAddressedBlobStore(store.data_root, store.namespace),
            store.repository,
        ),
    )
    with pytest.raises(AcquisitionNotFoundError, match="context"):
        bridge.prepare_execution(store.run.run_id)


def test_shared_attempt_snapshot_page_resume_and_stale_lease_fencing(acquisition_store):
    store = acquisition_store
    storage, _ = _initialize(store)
    _attach_existing_plan(store, storage)
    blob_store = ContentAddressedBlobStore(store.data_root, store.namespace)
    snapshot_service = SnapshotService(blob_store, store.repository)
    bridge = StructuredExecutionBridge(
        repository=store.repository,
        storage=storage,
        source_gate=CrossProcessSourceGate(store.data_root),
        snapshot_service=snapshot_service,
    )
    lease, old_token = bridge.claim_lease(
        store.run.run_id,
        owner_token="structured-owner-one",
        now=store.now,
        ttl_seconds=3600,
    )
    attempt = AcquisitionAttempt(
        attempt_id="structured-attempt-1",
        run_id=store.run.run_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        physical_query_plan_item_id=store.plan_item.plan_item_id,
        execution_key=store.plan_item.execution_key,
        attempt_kind="discovery",
        query_id=store.plan_item.query_id,
        time_start=store.plan_item.time_start,
        time_end=store.plan_item.time_end,
        page_number=1,
        work_position="page:1",
        retry_group_id="structured-retry-1",
        retry_ordinal=0,
        lease_epoch=lease.lease_epoch,
        started_at=store.now,
    )
    bridge.save_attempt(attempt, owner_token=old_token)
    frozen = snapshot_service.freeze_discovery_response(
        b'{"rows":[]}',
        DiscoverySnapshotRequest(
            attempt_id=attempt.attempt_id,
            physical_query_plan_item_id=store.plan_item.plan_item_id,
            source_definition_id=store.plan_item.source_definition_id,
            source_definition_version=store.plan_item.source_definition_version,
            query_page_canonical=f"{store.plan_item.plan_item_id}:page:1",
            mime_type="application/json",
            observed_at=store.now + timedelta(seconds=1),
            retrieved_at=store.now + timedelta(seconds=2),
            page_number=1,
            observation_id="structured-observation-1",
        ),
        owner_token=old_token,
        lease_epoch=lease.lease_epoch,
    )
    # Simulate a crash after the shared immutable response was saved but before
    # the structured projection was committed.
    assert storage.unprojected_snapshot_ids("job-1") == (frozen.snapshot.snapshot_id,)

    page = {
        "page_id": "structured-page-1",
        "job_id": "job-1",
        "attempt_id": attempt.attempt_id,
        "snapshot_id": frozen.snapshot.snapshot_id,
        "page_number": 1,
        "position_key": "page:1",
        "row_count": 501,
        "total_count": 501,
        "terminal": True,
        "content_hash": frozen.snapshot.sha256,
        "lease_epoch": lease.lease_epoch,
        "committed_at": store.now + timedelta(seconds=3),
    }
    records = tuple(
        {
            "record_version_id": f"structured-record-{index:03d}",
            "job_id": "job-1",
            "page_id": page["page_id"],
            "snapshot_id": frozen.snapshot.snapshot_id,
            "row_key": f"row:{index:03d}",
            "version_hash": hashlib.sha256(f"row:{index}".encode()).hexdigest(),
            "available_at": (store.now + timedelta(seconds=2)).isoformat(),
            "observed_at": (store.now + timedelta(seconds=1)).isoformat(),
        }
        for index in range(501)
    )
    fields = tuple(
        {
            "field_value_id": f"structured-field-{index:03d}",
            "record_version_id": record["record_version_id"],
            "dataset_id": "dataset-0",
            "raw_field_name": "VALUE",
            "field_path": "$.VALUE",
            "standard_field_id": "standard.value",
            "definition_version": "1.0.0",
            "nature": "reported",
            "quality": "supplier_reported",
            "value": index,
            "period_key": f"row:{index:03d}",
        }
        for index, record in enumerate(records)
    )
    bridge.commit_page_bundle(
        page=page,
        records=records,
        fields=fields,
        owner_token=old_token,
        lease_epoch=lease.lease_epoch,
    )
    bridge.commit_page_bundle(
        page=page,
        records=records,
        fields=fields,
        owner_token=old_token,
        lease_epoch=lease.lease_epoch,
    )
    assert len(storage.list_records(job_id="job-1", limit=None)) == 501
    assert len(
        storage.list_record_fields(
            standard_field_id="standard.value", limit=None
        )
    ) == 501
    assert storage.list_record_fields(
        record_version_id="structured-record-500", limit=None
    )[0]["value"] == 500
    assert len(storage.list_record_fields(limit=100, offset=500)) == 1
    assert storage.unprojected_snapshot_ids("job-1") == ()

    store.repository.append_attempt_event(
        AcquisitionAttemptEvent(
            event_id="structured-attempt-success",
            attempt_id=attempt.attempt_id,
            event_type="outcome_terminal",
            outcome="success",
            lease_epoch=lease.lease_epoch,
            occurred_at=store.now + timedelta(seconds=4),
        ),
        owner_token=old_token,
    )
    status = bridge.status(store.run.run_id)
    assert status.total_jobs == 1
    assert status.succeeded == 1
    assert status.jobs[0].committed_record_count == 501

    new_lease, _ = store.repository.claim_lease(
        store.run.run_id,
        owner_token="structured-owner-two",
        now=store.now + timedelta(hours=2),
        ttl_seconds=60,
    )
    assert new_lease.lease_epoch == lease.lease_epoch + 1
    with pytest.raises(StaleLeaseError):
        bridge.commit_page_bundle(
            page={**page, "page_id": "stale-page", "position_key": "page:2"},
            owner_token=old_token,
            lease_epoch=lease.lease_epoch,
        )
