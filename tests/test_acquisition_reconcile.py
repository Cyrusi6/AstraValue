from __future__ import annotations

from datetime import timedelta
import json
import pytest

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
    later_barrier_runtime,
)


def test_earliest_unresolved_barrier_exact_work_position_effective_overlap(tmp_path):
    runtime, adapter, parent, expected, state = later_barrier_runtime(tmp_path / "exact")
    before = tuple(runtime.repository.list_run_events(parent.run.run_id))
    plan = runtime.plan_company_run("600519", mode="reconcile", as_of=NOW,
                                    parent_run_id=parent.run.run_id)
    target = plan.run.reconcile_target
    assert target["selection_priority"] == 1
    assert target["plan_item_id"] == expected.plan_item_id
    assert json.loads(target["work_position"])["page"] == 3
    overlap = runtime.source_definition(expected.source_definition_id).incremental_policy.overlap_days
    assert target["effective_range"]["time_start"] == (expected.time_start - timedelta(days=overlap)).isoformat()
    assert {p.query_id for p in plan.physical_query_plan_items} == {expected.query_id}
    assert {p.source_definition_id for p in plan.physical_query_plan_items} == {expected.source_definition_id}
    assert min(p.time_start for p in plan.physical_query_plan_items) > min(p.time_start for p in parent.physical_query_plan_items)
    assert any(p.time_start == expected.time_start and p.time_end == expected.time_end
               for p in plan.physical_query_plan_items)
    state["blocked"] = False
    runtime.orchestrator.execute_run(plan.run.run_id)
    resolutions = runtime.repository.list_barrier_resolutions()
    assert target["barrier_id"] in {row.barrier_id for row in resolutions}
    assert all(row.work_position == target["work_position"] for row in resolutions)
    assert tuple(runtime.repository.list_run_events(parent.run.run_id)) == before
    import runpy
    from pathlib import Path
    validator = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/validate_acquisition_consistency.py"))
    validator["validate"](runtime.db_path, runtime.data_root)


def test_finalized_parent_rejected_before_child_plan_or_io(tmp_path):
    from orchestrator_support import no_data_adapter
    adapter = no_data_adapter()
    runtime = make_runtime(tmp_path / "unfinalized", adapter)
    parent = targeted_plan(runtime)
    with pytest.raises(ValueError, match="finalized"):
        runtime.plan_company_run("600519", mode="reconcile", parent_run_id=parent.run.run_id)
    assert len(runtime.repository.list_runs()) == 1 and adapter.query_calls == []


def test_shared_selector_latest_excludes_newer_unfinalized_run_no_gap_fallback(tmp_path):
    from orchestrator_support import no_data_adapter
    runtime = make_runtime(tmp_path / "latest", no_data_adapter())
    parent = targeted_plan(runtime)
    runtime.orchestrator.execute_run(parent.run.run_id)
    unfinished = targeted_plan(runtime)
    repair = runtime.plan_company_run("600519", mode="reconcile", as_of=NOW)
    assert repair.run.parent_run_id == parent.run.run_id
    assert repair.run.reconcile_target["selection_priority"] == 4
    assert repair.run.reconcile_target["excluded_newer_unfinalized_runs"] == [
        {"run_id": unfinished.run.run_id, "reason": "parent_not_finalized"}]


def test_incremental_requires_all_safe_checkpoints_before_run_creation(tmp_path):
    from orchestrator_support import no_data_adapter
    runtime = make_runtime(tmp_path / "incremental", no_data_adapter())
    parent = targeted_plan(runtime)
    runtime.orchestrator.execute_run(parent.run.run_id)
    count = len(runtime.repository.list_runs())
    with pytest.raises(ValueError, match="sse.disclosures"):
        runtime.plan_company_run("600519", mode="incremental", as_of=NOW)
    assert len(runtime.repository.list_runs()) == count


def test_shared_selector_unresolved_coverage_without_barrier_precedes_integrity(tmp_path, monkeypatch):
    from orchestrator_support import no_data_adapter
    runtime = make_runtime(tmp_path / "coverage-only", no_data_adapter())
    parent = targeted_plan(runtime)
    runtime.orchestrator.execute_run(parent.run.run_id)
    monkeypatch.setattr(runtime.repository, "list_coverage_resolutions", lambda run_id: [])
    selection = runtime.orchestrator.select_reconcile_target(parent.run.run_id, as_of=NOW)
    assert selection.target["strategy"] == "unresolved_coverage"
    assert selection.target["selection_priority"] == 2
    assert selection.target["barrier_id"] is None


def test_finalized_parent_with_active_lease_is_rejected(tmp_path, monkeypatch):
    from orchestrator_support import no_data_adapter
    runtime = make_runtime(tmp_path / "lease", no_data_adapter())
    parent = targeted_plan(runtime)
    runtime.orchestrator.execute_run(parent.run.run_id)
    lease = runtime.repository.get_lease(parent.run.run_id).model_copy(update={"released_at": None})
    monkeypatch.setattr(runtime.repository, "get_lease", lambda run_id: lease)
    with pytest.raises(ValueError, match="active lease"):
        runtime.plan_company_run("600519", mode="reconcile", parent_run_id=parent.run.run_id, as_of=NOW)
    assert len(runtime.repository.list_runs()) == 1


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
    assert selection.target["strategy"] == "earliest_unresolved_barrier"
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
