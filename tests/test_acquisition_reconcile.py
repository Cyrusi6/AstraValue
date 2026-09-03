from __future__ import annotations

from datetime import timedelta

from analysis.acquisition.adapters.base import FetchWork, QueryWork
from analysis.acquisition.models import SnapshotIntegrityEvent, SnapshotIntegrityStatus

from orchestrator_support import (
    NOW,
    ScenarioAdapter,
    discovery_result,
    envelope,
    make_runtime,
    resource,
    targeted_plan,
)


def test_gap_reconcile_resolves_exact_parent_barrier_without_mutating_parent(tmp_path) -> None:
    query_call = 0

    def response(work: QueryWork):
        nonlocal query_call
        query_call += 1
        if query_call == 1:
            return envelope(url=work.url, status=403)
        return envelope(url=work.url)

    adapter = ScenarioAdapter(
        response,
        lambda response, work: discovery_result(work),
    )
    runtime = make_runtime(tmp_path / "gap", adapter)
    parent = targeted_plan(runtime)
    parent_result = runtime.orchestrator.execute_run(parent.run.run_id)
    parent_events_before = tuple(runtime.repository.list_run_events(parent.run.run_id))
    parent_attempts_before = tuple(runtime.repository.list_attempts(run_id=parent.run.run_id))

    selection = runtime.orchestrator.select_reconcile_target(parent.run.run_id)

    assert parent_result.material_gap_count == 1
    assert selection.target["strategy"] == "earliest_unresolved_gap"
    assert len(selection.target["unresolved_barrier_ids"]) == 1
    parent_item = parent.physical_query_plan_items[0]
    repair = targeted_plan(
        runtime,
        mode="reconcile",
        start_at=parent_item.time_start,
        as_of=parent_item.time_end,
        parent_run_id=parent.run.run_id,
    )
    repaired = runtime.orchestrator.execute_run(repair.run.run_id)

    assert repaired.material_gap_count == 0
    assert runtime.repository.list_checkpoint_barriers(unresolved_only=True) == []
    resolutions = runtime.repository.list_barrier_resolutions()
    assert len(resolutions) == 1
    resolving = runtime.repository.get_attempt(resolutions[0].resolving_attempt_id)
    opening = runtime.repository.get_attempt(resolutions[0].opening_attempt_id)
    assert resolving.run_id == repair.run.run_id
    assert resolving.retry_group_id == opening.retry_group_id
    assert resolving.retry_ordinal > opening.retry_ordinal
    assert tuple(runtime.repository.list_run_events(parent.run.run_id)) == parent_events_before
    assert tuple(runtime.repository.list_attempts(run_id=parent.run.run_id)) == parent_attempts_before


def test_late_and_revision_reconcile_preserves_old_snapshot_version_chain(tmp_path) -> None:
    query_call = 0

    def response(work: QueryWork):
        nonlocal query_call
        query_call += 1
        return envelope(url=work.url)

    def parsed(_response, work: QueryWork):
        rows = (resource(),)
        if query_call > 1:
            rows += (
                resource(
                    "late-resource",
                    url="https://static.cninfo.com.cn/finalpage/late.pdf",
                    published_at=NOW - timedelta(days=30),
                ),
            )
        return discovery_result(work, resources=rows, declared_total=len(rows))

    def fetch(work: FetchWork):
        canonical = work.resource.canonical_resource_id
        body = (
            b"%PDF old"
            if query_call == 1
            else (
                b"%PDF revised" if canonical == "fixture-resource-1" else b"%PDF late"
            )
        )
        return envelope(
            url=work.query.url,
            body=body,
            content_type="application/pdf",
            headers={"etag": '"stable-etag"'},
        )

    adapter = ScenarioAdapter(response, parsed, fetch)
    runtime = make_runtime(tmp_path / "late-revision", adapter)
    parent = targeted_plan(runtime)
    runtime.orchestrator.execute_run(parent.run.run_id)
    old_snapshot = runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="fixture-resource-1",
    )
    old_payload = old_snapshot.model_dump_json()
    parent_item = parent.physical_query_plan_items[0]
    reconcile = targeted_plan(
        runtime,
        mode="reconcile",
        start_at=parent_item.time_start,
        as_of=parent_item.time_end,
        parent_run_id=parent.run.run_id,
    )

    result = runtime.orchestrator.execute_run(reconcile.run.run_id)

    revised = runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="fixture-resource-1",
    )
    late = runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="late-resource",
    )
    assert result.material_gap_count == 0
    assert revised.version == 2
    assert revised.supersedes_snapshot_id == old_snapshot.snapshot_id
    assert late.version == 1
    assert runtime.repository.get_raw_resource_snapshot(
        old_snapshot.snapshot_id
    ).model_dump_json() == old_payload


def test_integrity_failure_precedes_periodic_history_recheck(tmp_path) -> None:
    adapter = ScenarioAdapter(
        lambda work: envelope(url=work.url),
        lambda response, work: discovery_result(
            work, resources=(resource(),), declared_total=1
        ),
        lambda work: envelope(
            url=work.query.url,
            body=b"%PDF integrity fixture",
            content_type="application/pdf",
        ),
    )
    runtime = make_runtime(tmp_path / "integrity", adapter)
    parent = targeted_plan(runtime)
    result = runtime.orchestrator.execute_run(parent.run.run_id)

    periodic = runtime.orchestrator.select_reconcile_target(parent.run.run_id)
    assert periodic.target["strategy"] == (
        "earliest_completed_slice_periodic_history_recheck"
    )
    snapshot_id = next(
        observation.snapshot_id
        for attempt_id in result.attempt_ids
        for observation in runtime.repository.list_resource_observations(
            attempt_id=attempt_id
        )
        if observation.snapshot_id
    )
    runtime.repository.append_snapshot_integrity_event(
        SnapshotIntegrityEvent(
            snapshot_id=snapshot_id,
            status=SnapshotIntegrityStatus.QUARANTINED,
            reason_code="fixture_tamper",
        )
    )

    integrity = runtime.orchestrator.select_reconcile_target(parent.run.run_id)

    assert integrity.target["strategy"] == "snapshot_integrity_failure"
    assert integrity.target["quarantined_snapshot_ids"] == (snapshot_id,)


def test_registry_incompatibility_is_explicit_in_reconcile_target(tmp_path) -> None:
    adapter = ScenarioAdapter(
        lambda work: envelope(url=work.url),
        lambda response, work: discovery_result(work),
    )
    runtime = make_runtime(tmp_path / "registry", adapter)
    parent = targeted_plan(runtime)
    runtime.orchestrator.execute_run(parent.run.run_id)

    selection = runtime.orchestrator.select_reconcile_target(parent.run.run_id)

    # The targeted fixture freezes one source while the active v1 registry has
    # four, which is the same mismatch shape as a registry definition change.
    assert selection.target["registry_compatibility_review_required"] is True
