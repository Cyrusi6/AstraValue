from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from analysis.acquisition import source_gate as source_gate_module
from analysis.acquisition.adapters.base import (
    DiscoveryResult,
    QueryWork,
    TransportExecutionCapability,
)
from analysis.acquisition.models import (
    AcquisitionAttemptEvent,
    AcquisitionAttemptEventType,
    AcquisitionPlan,
    AcquisitionRun,
    AcquisitionRunKind,
    LiveAccessReviewCheck,
    SourceLiveAccessReview,
)
from analysis.acquisition.orchestrator import AcquisitionExecutionError
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH
from analysis.acquisition.repository import (
    AcquisitionNotFoundError,
    LeaseConflictError,
    StaleLeaseError,
)
from analysis.acquisition.transport import RegistryBoundHttpTransport

from orchestrator_support import (
    ScenarioAdapter,
    discovery_result,
    envelope,
    make_runtime,
    no_data_adapter,
    resource,
    targeted_plan,
)


@pytest.mark.parametrize(
    "run_kind",
    (
        AcquisitionRunKind.PRODUCTION,
        AcquisitionRunKind.SMOKE,
        AcquisitionRunKind.AD_HOC,
    ),
)
def test_production_smoke_ad_hoc_all_require_lease_and_conflict_has_zero_io(
    tmp_path, run_kind
) -> None:
    adapter = no_data_adapter()
    runtime = make_runtime(tmp_path / run_kind.value, adapter)
    plan = targeted_plan(runtime, run_kind=run_kind)
    lease, token = runtime.repository.claim_lease(
        plan.run.run_id,
        owner_token="holder",
        now=datetime.now(timezone.utc),
        ttl_seconds=60,
    )

    with pytest.raises(LeaseConflictError):
        runtime.orchestrator.execute_run(plan.run.run_id)

    assert adapter.query_calls == []
    runtime.repository.release_lease(
        plan.run.run_id,
        owner_token=token,
        lease_epoch=lease.lease_epoch,
        now=datetime.now(timezone.utc),
    )


def test_claim_before_io_and_execute_ttl_override(tmp_path) -> None:
    state = {}

    def before_io(_work: QueryWork) -> None:
        lease = runtime.repository.get_lease(plan.run.run_id)
        state["lease"] = lease
        capability = _work.execution_capability
        assert capability is not None
        state["capability"] = capability
        assert lease.released_at is None
        assert lease.is_active_at(datetime.now(timezone.utc))
        attempt = runtime.repository.get_attempt(capability.attempt_id)
        assert capability.run_id == plan.run.run_id == attempt.run_id
        assert capability.attempt_id == attempt.attempt_id
        assert capability.lease_epoch == lease.lease_epoch == attempt.lease_epoch
        assert capability.run_as_of == plan.run.as_of
        assert capability.source_definition_id == attempt.source_definition_id
        assert (
            capability.source_definition_version
            == attempt.source_definition_version
        )
        capability.guard(force=True)

    adapter = no_data_adapter(before_query=before_io)
    runtime = make_runtime(tmp_path / "claim-before-io", adapter)
    plan = targeted_plan(runtime)

    runtime.orchestrator.execute_run(plan.run.run_id, lease_ttl_seconds=5)

    assert state["lease"].ttl == timedelta(seconds=5)
    with pytest.raises(StaleLeaseError):
        state["capability"].guard(force=True)


@pytest.mark.parametrize(
    ("effective_at", "expires_at", "valid_as_of", "invalid_as_of", "message"),
    (
        (
            "2026-09-04T00:00:00Z",
            None,
            datetime(2026, 9, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 3, 23, 59, 59, tzinfo=timezone.utc),
            "尚未生效",
        ),
        (
            "2026-09-03T00:00:00Z",
            "2026-09-05T00:00:00Z",
            datetime(2026, 9, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 5, tzinfo=timezone.utc),
            "已经失效",
        ),
    ),
)
def test_executor_rechecks_frozen_definition_effective_window_before_lease_attempt_or_io(
    tmp_path, effective_at, expires_at, valid_as_of, invalid_as_of, message
) -> None:
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    definition_payload = payload["definitions"][0]
    definition_payload["effective_at"] = effective_at
    definition_payload["expires_at"] = expires_at
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    adapter = no_data_adapter()
    runtime = make_runtime(
        tmp_path / "effective-execution",
        adapter,
        registry_path=registry_path,
    )
    original = targeted_plan(
        runtime,
        start_at=valid_as_of - timedelta(hours=1),
        as_of=valid_as_of,
        persist=False,
    )
    invalid_run = AcquisitionRun.model_validate(
        {**original.run.model_dump(mode="python"), "as_of": invalid_as_of}
    )
    invalid_plan = AcquisitionPlan(
        run=invalid_run,
        coverage_entries=original.coverage_entries,
        physical_query_plan_items=original.physical_query_plan_items,
        coverage_links=original.coverage_links,
    )
    runtime.orchestrator.persist_plan(invalid_plan)

    with pytest.raises(AcquisitionExecutionError, match=message):
        runtime.orchestrator.execute_run(invalid_run.run_id)

    assert runtime.repository.list_attempts(run_id=invalid_run.run_id) == []
    with pytest.raises(AcquisitionNotFoundError):
        runtime.repository.get_lease(invalid_run.run_id)
    assert adapter.query_calls == []


def test_pending_manual_review_becomes_policy_skip_attempt_with_zero_client_send(
    tmp_path,
) -> None:
    send_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        send_calls.append(str(request.url))
        return httpx.Response(200, json={"data": [], "total": 0}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    from analysis.acquisition.runtime import AcquisitionRuntime

    runtime = AcquisitionRuntime.create(
        tmp_path / "pending-review.db",
        tmp_path / "pending-review-data",
        workspace_root=tmp_path / "workspace",
        http_client=client,
    )
    plan = targeted_plan(runtime)
    try:
        result = runtime.orchestrator.execute_run(plan.run.run_id)
    finally:
        client.close()

    attempts = runtime.repository.list_attempts(run_id=plan.run.run_id)
    terminal_events = [
        event
        for attempt in attempts
        for event in runtime.repository.list_attempt_events(attempt.attempt_id)
        if event.event_type == AcquisitionAttemptEventType.OUTCOME_TERMINAL
    ]
    assert send_calls == []
    assert result.material_gap_count > 0
    assert len(attempts) == 1
    assert terminal_events[0].outcome.value == "policy_skipped"
    assert terminal_events[0].reason_code == "manual_access_review_required"


def test_heartbeat_renews_during_network_io(tmp_path) -> None:
    observed = {}

    def slow_response(work: QueryWork):
        observed["before"] = runtime.repository.get_lease(plan.run.run_id).heartbeat_at
        time.sleep(0.18)
        observed["after"] = runtime.repository.get_lease(plan.run.run_id).heartbeat_at
        return envelope(
            url=work.url,
            observed_at=datetime.now(timezone.utc),
        )

    adapter = ScenarioAdapter(
        slow_response,
        lambda response, work: discovery_result(work),
    )
    runtime = make_runtime(
        tmp_path / "heartbeat",
        adapter,
        now=lambda: datetime.now(timezone.utc),
        lease_ttl_seconds=1,
        heartbeat_interval_seconds=0.05,
    )
    plan = targeted_plan(runtime)

    runtime.orchestrator.execute_run(plan.run.run_id)

    assert observed["after"] > observed["before"]


def test_gate_wait_stale_owner_fenced_before_transport_send(
    tmp_path, monkeypatch
) -> None:
    runtime = make_runtime(tmp_path / "gate-stale-owner", no_data_adapter())
    plan = targeted_plan(runtime)
    plan_item = plan.physical_query_plan_items[0]
    definition = runtime.source_definition(
        plan_item.source_definition_id,
        plan_item.source_definition_version,
    )
    definition = definition.model_copy(
        update={
            "live_access_review": SourceLiveAccessReview(
                status="approved",
                checklist_version="business_model_v1.9.1",
                completed_checks=tuple(LiveAccessReviewCheck),
                reviewed_at=datetime.now(timezone.utc),
                reviewed_by="fixture-reviewer",
                evidence_reference="fixture:source-gate-fencing",
            )
        }
    )
    query = next(item for item in definition.queries if item.query_id == plan_item.query_id)
    claimed_at = datetime.now(timezone.utc)
    old_lease, old_token = runtime.repository.claim_lease(
        plan.run.run_id,
        owner_token="gate-old-owner",
        now=claimed_at,
        ttl_seconds=1,
    )
    reclaimed = []
    send_calls = []

    def lease_guard(*, force: bool = False) -> None:
        runtime.repository.renew_lease(
            plan.run.run_id,
            owner_token=old_token,
            lease_epoch=old_lease.lease_epoch,
            now=datetime.now(timezone.utc),
            ttl_seconds=1,
        )

    def busy_then_reclaim(_handle) -> None:
        lease, _token = runtime.repository.claim_lease(
            plan.run.run_id,
            owner_token="gate-new-owner",
            now=claimed_at + timedelta(seconds=2),
            ttl_seconds=60,
        )
        reclaimed.append(lease)
        raise OSError("simulated busy source gate")

    def handler(_request: httpx.Request) -> httpx.Response:
        send_calls.append(1)
        return httpx.Response(200, json={"data": [], "total": 0})

    monkeypatch.setattr(source_gate_module, "_lock_one_byte", busy_then_reclaim)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = RegistryBoundHttpTransport(
        definition,
        runtime.source_gate,
        client=client,
        address_resolver=lambda _host, _port: ("8.8.8.8",),
    )
    is_get = plan_item.request_method == "GET"
    work = QueryWork(
        source_definition_id=definition.source_definition_id,
        source_definition_version=definition.version,
        query_id=query.query_id,
        query_family=query.query_family,
        execution_key=plan_item.execution_key,
        method=plan_item.request_method,
        url=plan_item.endpoint,
        params=(plan_item.normalized_parameters if is_get else {}),
        form_body=(None if is_get else plan_item.normalized_parameters),
        expected_mime_types=tuple(query.discovery_schema.response_mime_types),
        max_response_bytes=definition.response_limits.max_response_bytes,
        parser_schema_version=query.discovery_schema.schema_version,
        context={"deadline_monotonic": time.monotonic() + 5},
        execution_capability=TransportExecutionCapability(
            run_id=plan.run.run_id,
            attempt_id="gate-stale-attempt",
            lease_epoch=old_lease.lease_epoch,
            run_as_of=plan.run.as_of,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            lease_guard=lease_guard,
        ),
    )

    try:
        with pytest.raises(StaleLeaseError):
            transport.request(work)
    finally:
        client.close()

    assert reclaimed[0].lease_epoch == old_lease.lease_epoch + 1
    assert send_calls == []


def test_crash_after_page_two_is_abandoned_and_resumes_without_repeating_pages(
    tmp_path,
) -> None:
    calls: list[int] = []
    crash_once = True
    rows = {
        1: (resource("page-one", required_fetch=False),),
        2: (resource("page-two", required_fetch=False),),
        3: (),
    }

    def response(work: QueryWork):
        nonlocal crash_once
        calls.append(work.page)
        if work.page == 3 and crash_once:
            crash_once = False
            raise KeyboardInterrupt("simulated process interruption")
        return envelope(
            url=work.url,
            body=f"page={work.page}".encode(),
            observed_at=datetime.now(timezone.utc),
        )

    def parsed(_response, work: QueryWork) -> DiscoveryResult:
        return discovery_result(
            work,
            resources=rows[work.page],
            declared_total=2,
            page_count=3,
            terminal=work.page == 3,
        )

    adapter = ScenarioAdapter(response, parsed)
    runtime = make_runtime(
        tmp_path / "resume",
        adapter,
        now=lambda: datetime.now(timezone.utc),
    )
    plan = targeted_plan(runtime)

    with pytest.raises(KeyboardInterrupt):
        runtime.orchestrator.execute_run(plan.run.run_id)
    first_attempt = runtime.repository.list_attempts(run_id=plan.run.run_id)[0]
    assert [
        item.page_number
        for item in runtime.repository.list_attempt_segments(first_attempt.attempt_id)
    ] == [1, 2]

    result = runtime.orchestrator.execute_run(plan.run.run_id)

    attempts = runtime.repository.list_attempts(run_id=plan.run.run_id)
    resumed = max(attempts, key=lambda item: item.retry_ordinal)
    first_events = runtime.repository.list_attempt_events(first_attempt.attempt_id)
    assert calls == [1, 2, 3, 3]
    assert any(
        event.event_type == AcquisitionAttemptEventType.ABANDONED
        for event in first_events
    )
    assert all(event.outcome is None for event in first_events)
    assert resumed.supersedes_attempt_id == first_attempt.attempt_id
    assert [
        item.page_number
        for item in runtime.repository.list_attempt_segments(resumed.attempt_id)
    ] == [3]
    assert result.material_gap_count == 0


def test_stale_owner_is_fenced_after_expired_reclaim(tmp_path) -> None:
    adapter = no_data_adapter()
    runtime = make_runtime(tmp_path / "stale", adapter)
    plan = targeted_plan(runtime)
    claimed_at = datetime.now(timezone.utc)
    first, first_token = runtime.repository.claim_lease(
        plan.run.run_id,
        owner_token="old-owner",
        now=claimed_at,
        ttl_seconds=1,
    )
    second, _second_token = runtime.repository.claim_lease(
        plan.run.run_id,
        owner_token="new-owner",
        now=claimed_at + timedelta(seconds=2),
        ttl_seconds=60,
    )

    with pytest.raises(StaleLeaseError):
        runtime.repository.append_run_event(
            {
                "event_id": "late-old-owner",
                "run_id": plan.run.run_id,
                "event_type": "running",
                "occurred_at": claimed_at + timedelta(seconds=3),
                "lease_epoch": first.lease_epoch,
            },
            owner_token=first_token,
        )

    assert second.lease_epoch == first.lease_epoch + 1
    assert adapter.query_calls == []


def test_smoke_sources_is_registry_driven_and_never_advances_checkpoint(tmp_path) -> None:
    adapter = no_data_adapter()
    runtime = make_runtime(tmp_path / "smoke-sources", adapter)

    result = runtime.orchestrator.smoke_sources(
        ticker="600519",
        source_ids=["cninfo.disclosures"],
    )

    run = runtime.repository.get_run(result.run_id)
    plan_items = runtime.repository.list_physical_query_plan_items(run.run_id)
    smoke_query_ids = {
        query.query_id
        for query in runtime.source_definition("cninfo.disclosures").queries
        if query.smoke_enabled
    }
    assert run.run_kind == AcquisitionRunKind.SMOKE
    assert {item.query_id for item in plan_items} == smoke_query_ids
    assert runtime.repository.latest_checkpoint(
        run.ticker,
        "cninfo.disclosures",
        "1.0.0",
        run.question_set_version,
    ) is None
