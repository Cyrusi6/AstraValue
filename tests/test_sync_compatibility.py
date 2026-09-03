from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from analysis.acquisition.models import (
    AcquisitionAttempt,
    AcquisitionAttemptEvent,
    AcquisitionAttemptEventType,
    AcquisitionMode,
    AcquisitionOutcome,
    AcquisitionRunEvent,
    AcquisitionRunEventType,
)
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.adapters.manager import AdapterManager
from analysis.models import SyncResult
from analysis.storage import ReportStorage


NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


def test_legacy_json_is_unassessed_but_remains_consumable(tmp_path):
    storage = ReportStorage(tmp_path / "analysis.db")
    payload = {
        "sync_result_id": "legacy-sync",
        "ticker": "600519",
        "provider_results": {"official": "historical text only"},
        "scopes": ["financials"],
        "as_of": NOW.isoformat(),
        "created_at": NOW.isoformat(),
    }
    with storage._connect() as connection:
        connection.execute(
            "INSERT INTO sync_results(sync_result_id,ticker,as_of,created_at,payload) "
            "VALUES (?,?,?,?,?)",
            (
                payload["sync_result_id"],
                payload["ticker"],
                payload["as_of"],
                payload["created_at"],
                json.dumps(payload),
            ),
        )
    loaded = storage.get_sync_result("legacy-sync")
    assert loaded.acquisition_status == "legacy_unassessed"
    assert loaded.provider_results_authority == "legacy_unassessed"
    assert loaded.default_consume_eligible is True
    assert storage.latest_sync_result("600519").sync_result_id == "legacy-sync"


def test_new_partial_does_not_shadow_older_default_consumable_batch(tmp_path):
    storage = ReportStorage(tmp_path / "analysis.db")
    eligible = SyncResult(
        sync_result_id="eligible-sync",
        ticker="600519",
        provider_results={"official": "deprecated summary"},
        acquisition_run_id="run-eligible",
        acquisition_status="succeeded",
        provider_results_authority="structured_attempts",
        coverage_accounted=True,
        material_gap_count=0,
        default_consume_eligible=True,
        as_of=NOW,
        created_at=NOW,
    )
    partial = SyncResult(
        sync_result_id="partial-sync",
        ticker="600519",
        provider_results={"official": "deprecated partial summary"},
        acquisition_run_id="run-partial",
        acquisition_status="partial",
        provider_results_authority="structured_attempts",
        coverage_accounted=True,
        material_gap_count=1,
        default_consume_eligible=False,
        as_of=NOW + timedelta(hours=1),
        created_at=NOW + timedelta(hours=1),
    )
    storage.save_sync_result(eligible)
    storage.save_sync_result(partial)
    assert storage.get_sync_result("partial-sync") == partial
    assert storage.latest_sync_result("600519") == eligible


def test_failed_baseline_without_safe_checkpoints_cannot_select_incremental(tmp_path):
    runtime = AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "evidence",
        orchestrator_factory=lambda _runtime: None,
    )
    plan = runtime.plan_company_run(
        "600519",
        mode=AcquisitionMode.BASELINE,
        as_of=NOW,
        listing_date=datetime(2001, 8, 27).date(),
    )
    runtime.repository.append_run_event(
        AcquisitionRunEvent(
            run_id=plan.run.run_id,
            event_type=AcquisitionRunEventType.FINALIZED,
            occurred_at=NOW + timedelta(seconds=1),
            result="failed",
            coverage_accounted=True,
            material_gap_count=len(plan.coverage_entries),
            default_consume_eligible=False,
            reason_code="fixture_failed_baseline",
        )
    )

    assert runtime.default_business_model_mode("600519") == AcquisitionMode.BASELINE
    with pytest.raises(ValueError, match="不存在兼容且安全的checkpoint"):
        runtime.plan_company_run(
            "600519",
            mode=AcquisitionMode.INCREMENTAL,
            as_of=NOW + timedelta(days=1),
        )


def test_provider_summary_is_deterministically_derived_from_attempt_events(tmp_path):
    lease_now = datetime.now(timezone.utc)
    runtime = AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "evidence",
        orchestrator_factory=lambda _runtime: None,
    )
    plan = runtime.plan_company_run(
        "600519",
        mode=AcquisitionMode.BASELINE,
        as_of=NOW,
        listing_date=datetime(2001, 8, 27).date(),
    )
    plan_item = next(
        item
        for item in plan.physical_query_plan_items
        if item.attempt_kind.value == "discovery"
    )
    lease, owner_token = runtime.repository.claim_lease(
        plan.run.run_id,
        now=lease_now,
        ttl_seconds=600,
    )
    attempt = AcquisitionAttempt(
        attempt_id="attempt-sync-summary",
        run_id=plan.run.run_id,
        source_definition_id=plan_item.source_definition_id,
        source_definition_version=plan_item.source_definition_version,
        physical_query_plan_item_id=plan_item.plan_item_id,
        execution_key=plan_item.execution_key,
        attempt_kind=plan_item.attempt_kind,
        query_id=plan_item.query_id,
        time_start=plan_item.time_start,
        time_end=plan_item.time_end,
        work_position="page:1",
        retry_group_id="retry-sync-summary",
        lease_epoch=lease.lease_epoch,
        started_at=lease_now,
    )
    runtime.repository.save_attempt(attempt, owner_token=owner_token)
    runtime.repository.append_attempt_event(
        AcquisitionAttemptEvent(
            attempt_id=attempt.attempt_id,
            event_type=AcquisitionAttemptEventType.OUTCOME_TERMINAL,
            occurred_at=lease_now + timedelta(seconds=1),
            lease_epoch=lease.lease_epoch,
            outcome=AcquisitionOutcome.RESTRICTED,
            reason_code="fixture_restricted",
        ),
        owner_token=owner_token,
    )
    manager = AdapterManager(
        loaded_registry=runtime.loaded_registry,
        acquisition_runtime=runtime,
    )

    first = manager._executed_source_summaries(plan.run.run_id)
    second = manager._executed_source_summaries(plan.run.run_id)

    assert first == second
    summary = json.loads(first[plan_item.source_definition_id])
    assert summary["kind"] == "attempts"
    assert summary["counts"]["restricted"] == 1
    assert "fixture_restricted" not in first[plan_item.source_definition_id]


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_results_authority": "legacy_unassessed"},
        {"acquisition_status": "legacy_unassessed"},
        {"coverage_accounted": False},
        {"material_gap_count": 1},
    ],
)
def test_acquisition_sync_result_rejects_inconsistent_authority_or_eligibility(
    overrides,
):
    values = {
        "ticker": "600519",
        "provider_results": {},
        "acquisition_run_id": "run-structured",
        "acquisition_status": "succeeded",
        "provider_results_authority": "structured_attempts",
        "coverage_accounted": True,
        "material_gap_count": 0,
        "default_consume_eligible": True,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        SyncResult(**values)
