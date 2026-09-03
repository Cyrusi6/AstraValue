from __future__ import annotations

from datetime import timedelta

import httpx

from analysis.acquisition.adapters.base import DiscoveryResult, QueryWork
from analysis.acquisition.models import AcquisitionMode, AcquisitionRunEventType
from analysis.acquisition.repository import AcquisitionRepository

from orchestrator_support import (
    NOW,
    ScenarioAdapter,
    discovery_result,
    envelope,
    make_runtime,
    no_data_adapter,
    resource,
    targeted_plan,
)


def test_persist_before_io_coverage_matrix_ten_topic_traceability_shared_execution_no_duplicate_io(
    tmp_path,
) -> None:
    state: dict[str, object] = {}

    def assert_durable_before_first_io(_work: QueryWork) -> None:
        if state.get("checked"):
            return
        runtime = state["runtime"]
        plan = state["plan"]
        reader = AcquisitionRepository(runtime.db_path, initialize=False)
        assert reader.get_run(plan.run.run_id) == plan.run
        assert reader.list_physical_query_plan_items(plan.run.run_id)
        assert len(reader.list_coverage_entries(plan.run.run_id)) == len(
            plan.coverage_entries
        )
        assert len(
            reader.list_physical_query_coverage_links(run_id=plan.run.run_id)
        ) == len(plan.coverage_links)
        assert any(
            event.event_type == AcquisitionRunEventType.PLANNED
            for event in reader.list_run_events(plan.run.run_id)
        )
        state["checked"] = True

    adapter = no_data_adapter(before_query=assert_durable_before_first_io)
    runtime = make_runtime(tmp_path / "persist-before-io", adapter)
    profile = runtime.build_profile(
        "600519", company_name="贵州茅台", listing_date=(NOW - timedelta(days=1)).date()
    )
    plan = runtime.planner.plan(
        profile,
        mode=AcquisitionMode.INCREMENTAL,
        start_at=NOW - timedelta(days=1),
        as_of=NOW,
        storage_namespace_id=runtime.namespace_id,
    )
    state.update(runtime=runtime, plan=plan)

    result = runtime.orchestrator.execute_plan(plan)

    assert state["checked"] is True
    assert result.coverage_accounted is True
    assert result.material_gap_count == 0
    assert len(adapter.query_calls) == len(plan.physical_query_plan_items)
    assert len({call.execution_key for call in adapter.query_calls}) == len(
        adapter.query_calls
    )
    assert {entry.question_id for entry in plan.coverage_entries} == {
        topic.question_id for topic in runtime.loaded_questions.question_set.topics
    }
    shared = next(
        item
        for item in plan.physical_query_plan_items
        if item.query_id == "cninfo.periodic_report"
    )
    assert sum(link.plan_item_id == shared.plan_item_id for link in plan.coverage_links) == 10


def test_pagination_uses_all_page_proofs_and_finishes_once(tmp_path) -> None:
    one = resource(required_fetch=False)

    def parsed(_response, work: QueryWork) -> DiscoveryResult:
        if work.page == 1:
            return discovery_result(
                work,
                resources=(one,),
                declared_total=1,
                page_count=2,
                terminal=False,
            )
        return discovery_result(
            work,
            declared_total=1,
            page_count=2,
            terminal=True,
        )

    adapter = ScenarioAdapter(
        lambda work: envelope(url=work.url, body=f"page={work.page}".encode()),
        parsed,
    )
    runtime = make_runtime(tmp_path / "pagination", adapter)
    plan = targeted_plan(runtime)

    result = runtime.orchestrator.execute_run(plan.run.run_id)

    assert result.material_gap_count == 0
    assert [call.page for call in adapter.query_calls] == [1, 2]
    attempts = runtime.repository.list_attempts(run_id=plan.run.run_id)
    assert len(attempts) == 1
    assert len(runtime.repository.list_attempt_segments(attempts[0].attempt_id)) == 2


def test_retry_is_a_new_attempt_and_success_resolves_opening_barrier(tmp_path) -> None:
    calls = 0

    def response(work: QueryWork):
        nonlocal calls
        calls += 1
        if calls == 1:
            return envelope(
                url=work.url,
                status=429,
                headers={"retry-after": "0"},
            )
        return envelope(url=work.url)

    adapter = ScenarioAdapter(
        response,
        lambda response, work: discovery_result(work),
    )
    runtime = make_runtime(tmp_path / "retry", adapter, sleep=lambda _delay: None)
    plan = targeted_plan(runtime)

    result = runtime.orchestrator.execute_run(plan.run.run_id)

    attempts = runtime.repository.list_attempts(run_id=plan.run.run_id)
    assert len(attempts) == 2
    assert attempts[1].supersedes_attempt_id == attempts[0].attempt_id
    assert result.outcome_counts == {"no_data": 1, "rate_limited": 1}
    assert result.material_gap_count == 0
    assert runtime.repository.list_checkpoint_barriers(unresolved_only=True) == []
    assert len(runtime.repository.list_barrier_resolutions()) == 1


def test_retry_after_deadline_stops_without_unbounded_wait(tmp_path) -> None:
    adapter = ScenarioAdapter(
        lambda work: envelope(
            url=work.url,
            status=429,
            headers={"retry-after": "999"},
        ),
        lambda response, work: discovery_result(work),
    )
    runtime = make_runtime(tmp_path / "retry-deadline", adapter)
    plan = targeted_plan(runtime)

    result = runtime.orchestrator.execute_run(plan.run.run_id)

    assert len(adapter.query_calls) == 1
    assert result.outcome_counts == {"rate_limited": 1}
    assert result.material_gap_count == 1


def test_no_fake_timeout_when_read_error_precedes_deadline(tmp_path) -> None:
    def fail(_work: QueryWork):
        raise httpx.ReadTimeout("fixture interrupted")

    adapter = ScenarioAdapter(
        fail,
        lambda response, work: discovery_result(work),
    )
    runtime = make_runtime(tmp_path / "no-fake-timeout", adapter)
    plan = targeted_plan(runtime)

    result = runtime.orchestrator.execute_run(plan.run.run_id)

    assert result.outcome_counts == {"network_failed": 3}
    assert "timeout" not in result.outcome_counts
