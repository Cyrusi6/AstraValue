from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analysis.acquisition.adapters.base import FetchWork, QueryWork
from analysis.acquisition.models import AcquisitionAttemptEventType

from orchestrator_support import (
    NOW,
    ScenarioAdapter,
    discovery_result,
    envelope,
    make_runtime,
    resource,
    targeted_plan,
)


def test_baseline_two_incrementals_and_reconcile_end_to_end(tmp_path) -> None:
    query_calls = 0
    fetch_calls = 0
    interrupt_once = True
    pdf_v1 = b"%PDF e2e v1"
    pdf_v2 = b"%PDF e2e v2"

    def response(work: QueryWork):
        nonlocal query_calls, interrupt_once
        query_calls += 1
        if work.page == 3 and interrupt_once:
            interrupt_once = False
            raise KeyboardInterrupt("fixture crash after baseline page two")
        return envelope(
            url=work.url,
            body=f"query={query_calls};page={work.page}".encode(),
            observed_at=datetime.now(timezone.utc),
        )

    def parsed(_response, work: QueryWork):
        if query_calls <= 4:
            rows = (
                (resource(),) if work.page == 2 else ()
            )
            return discovery_result(
                work,
                resources=rows,
                declared_total=1,
                page_count=3,
                terminal=work.page == 3,
            )
        rows = (resource(),)
        if query_calls >= 7:
            rows += (
                resource(
                    "late-e2e",
                    url="https://static.cninfo.com.cn/finalpage/late-e2e.pdf",
                    published_at=NOW - timedelta(days=60),
                ),
            )
        return discovery_result(work, resources=rows, declared_total=len(rows))

    def fetch(work: FetchWork):
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls == 2:
            return envelope(
                url=work.query.url,
                body=b"",
                status=304,
                content_type="application/pdf",
                headers={"etag": '"e2e-v1"'},
                observed_at=datetime.now(timezone.utc),
            )
        if work.resource.canonical_resource_id == "late-e2e":
            body = b"%PDF late e2e"
            etag = '"late"'
        elif fetch_calls == 1:
            body = pdf_v1
            etag = '"e2e-v1"'
        else:
            body = pdf_v2
            etag = '"e2e-v1"'
        return envelope(
            url=work.query.url,
            body=body,
            content_type="application/pdf",
            headers={"etag": etag},
            observed_at=datetime.now(timezone.utc),
        )

    adapter = ScenarioAdapter(response, parsed, fetch)
    runtime = make_runtime(
        tmp_path / "e2e",
        adapter,
        now=lambda: datetime.now(timezone.utc),
    )
    baseline = targeted_plan(
        runtime,
        mode="baseline",
        start_at=NOW - timedelta(days=1),
        as_of=NOW,
    )

    with pytest.raises(KeyboardInterrupt):
        runtime.orchestrator.execute_run(baseline.run.run_id)
    baseline_result = runtime.orchestrator.execute_run(baseline.run.run_id)

    assert len(baseline.coverage_links) == 10
    assert baseline_result.material_gap_count == 0
    assert baseline_result.manifest_id is not None
    assert baseline_result.checkpoint_ids
    baseline_attempts = tuple(
        runtime.repository.list_attempts(run_id=baseline.run.run_id)
    )
    abandoned = [
        attempt
        for attempt in baseline_attempts
        if any(
            event.event_type == AcquisitionAttemptEventType.ABANDONED
            for event in runtime.repository.list_attempt_events(attempt.attempt_id)
        )
    ]
    assert len(abandoned) == 1
    assert len(
        [
            proof
            for attempt in baseline_attempts
            for proof in runtime.repository.list_discovery_proofs(attempt.attempt_id)
        ]
    ) == 3
    baseline_events_frozen = tuple(
        runtime.repository.list_run_events(baseline.run.run_id)
    )
    abandoned_events_frozen = tuple(
        runtime.repository.list_attempt_events(abandoned[0].attempt_id)
    )

    incremental_one = targeted_plan(
        runtime,
        start_at=NOW - timedelta(hours=6),
        as_of=NOW + timedelta(hours=1),
    )
    first_incremental_result = runtime.orchestrator.execute_run(
        incremental_one.run.run_id
    )
    incremental_two = targeted_plan(
        runtime,
        start_at=NOW - timedelta(hours=6),
        as_of=NOW + timedelta(hours=2),
    )
    second_incremental_result = runtime.orchestrator.execute_run(
        incremental_two.run.run_id
    )

    assert first_incremental_result.outcome_counts["unchanged"] == 1
    assert second_incremental_result.outcome_counts["success"] == 2
    latest_before_reconcile = runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="fixture-resource-1",
    )
    assert latest_before_reconcile.version == 2

    selection = runtime.orchestrator.select_reconcile_target(baseline.run.run_id)
    baseline_item = baseline.physical_query_plan_items[0]
    reconcile = targeted_plan(
        runtime,
        mode="reconcile",
        start_at=baseline_item.time_start,
        as_of=baseline_item.time_end,
        parent_run_id=baseline.run.run_id,
    )
    reconcile_result = runtime.orchestrator.execute_run(reconcile.run.run_id)

    assert selection.target["strategy"] == (
        "earliest_completed_slice_periodic_history_recheck"
    )
    assert reconcile_result.material_gap_count == 0
    assert runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="late-e2e",
    ) is not None
    assert tuple(runtime.repository.list_run_events(baseline.run.run_id)) == (
        baseline_events_frozen
    )
    assert tuple(
        runtime.repository.list_attempt_events(abandoned[0].attempt_id)
    ) == abandoned_events_frozen


def test_required_fetch_failure_is_repaired_by_exact_successor_barrier_resolution(
    tmp_path,
) -> None:
    fetch_calls = 0

    def fetch(work: FetchWork):
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls == 1:
            return envelope(
                url=work.query.url,
                status=403,
                content_type="application/pdf",
            )
        return envelope(
            url=work.query.url,
            body=b"%PDF repaired attachment",
            content_type="application/pdf",
        )

    adapter = ScenarioAdapter(
        lambda work: envelope(url=work.url),
        lambda response, work: discovery_result(
            work, resources=(resource(),), declared_total=1
        ),
        fetch,
    )
    runtime = make_runtime(tmp_path / "required-fetch", adapter)
    parent = targeted_plan(runtime)
    parent_result = runtime.orchestrator.execute_run(parent.run.run_id)
    parent_item = parent.physical_query_plan_items[0]
    reconcile = targeted_plan(
        runtime,
        mode="reconcile",
        start_at=parent_item.time_start,
        as_of=parent_item.time_end,
        parent_run_id=parent.run.run_id,
    )

    repaired = runtime.orchestrator.execute_run(reconcile.run.run_id)

    assert parent_result.material_gap_count == 1
    assert repaired.material_gap_count == 0
    assert len(runtime.repository.list_barrier_resolutions()) == 1
    assert runtime.repository.list_checkpoint_barriers(unresolved_only=True) == []
    parent_discovery = next(
        attempt
        for attempt in runtime.repository.list_attempts(run_id=parent.run.run_id)
        if attempt.attempt_kind.value == "discovery"
    )
    terminal = next(
        event
        for event in runtime.repository.list_attempt_events(parent_discovery.attempt_id)
        if event.outcome is not None
    )
    assert terminal.outcome.value == "success"
