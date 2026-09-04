from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from analysis.acquisition.adapters.base import DiscoveryResult, QueryWork
from analysis.acquisition.models import AcquisitionMode, AcquisitionRunEventType
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH
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


class RegistryProtocolAdapter:
    def __init__(self) -> None:
        self.query_calls = []
        self.fetch_calls = []

    def execute_query(self, work):
        self.query_calls.append(work)
        return envelope(url=work.url)

    def parse_retained_discovery(self, _snapshot_id, work):
        if work.query_family == "company_bootstrap":
            company = resource(
                canonical_id="cninfo-org:600519:gssh0600519",
                url=work.url,
                required_fetch=False,
            )
            company = replace(
                company,
                metadata={
                    "ticker": "600519",
                    "org_id": "gssh0600519",
                    "wire_stock": "600519,gssh0600519",
                },
            )
            return discovery_result(
                work,
                resources=(company,),
                declared_total=1,
            )
        resource_url = {
            "cninfo.disclosures": "https://static.cninfo.com.cn/finalpage/fixture.pdf",
            "sse.disclosures": (
                "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
                "c/fixture.pdf"
            ),
            "szse.disclosures": "https://disc.static.szse.cn/download/fixture.pdf",
        }[work.source_definition_id]
        item = resource(
            canonical_id=f"{work.source_definition_id}:fixture",
            url=resource_url,
            required_fetch=work.context["fetch_policy"] == "required_attachment",
        )
        return discovery_result(work, resources=(item,), declared_total=1)

    def validate_and_normalize_without_retention(self, _envelope, work):
        return self.parse_retained_discovery("unused", work)

    def fetch_resource(self, work):
        self.fetch_calls.append(work)
        raise AssertionError("metadata-only smoke must not fetch attachments")


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


def test_cninfo_v1_2_smoke_resolves_bootstrap_binding_into_form_body(tmp_path) -> None:
    adapter = RegistryProtocolAdapter()
    runtime = make_runtime(
        tmp_path / "cninfo-v1-2-wire",
        adapter,
        registry_path=DEFAULT_REGISTRY_PATH,
    )
    as_of = max(
        definition.effective_at
        for definition in runtime.loaded_registry.registry.definitions
    ) + timedelta(seconds=1)
    try:
        result = runtime.orchestrator.smoke_sources(
            ticker="600519",
            source_ids=["cninfo.disclosures"],
            as_of=as_of,
        )

        assert [work.query_id for work in adapter.query_calls] == [
            "cninfo.company_bootstrap",
            "cninfo.periodic_report",
        ]
        bootstrap, periodic = adapter.query_calls
        assert bootstrap.method == "GET"
        assert bootstrap.params == {}
        assert bootstrap.form_body is None
        assert periodic.method == "POST"
        assert periodic.params == {}
        assert periodic.form_body["stock"] == "600519,gssh0600519"
        assert periodic.form_body["pageNum"] == 1
        assert periodic.form_body["pageSize"] == 30
        assert periodic.form_body["plate"] == "sh"
        assert set(periodic.headers) == {
            "Accept",
            "Referer",
            "User-Agent",
            "X-Requested-With",
        }
        assert adapter.fetch_calls == []
        assert result.material_gap_count == 0
        assert result.checkpoint_advanced is False
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("source_id", "ticker", "expected_encoding"),
    (
        ("sse.disclosures", "600519", "query"),
        ("szse.disclosures", "300750", "json"),
    ),
)
def test_v1_2_exchange_smoke_uses_registered_wire_shape_and_no_fetch(
    tmp_path, source_id, ticker, expected_encoding
) -> None:
    adapter = RegistryProtocolAdapter()
    runtime = make_runtime(
        tmp_path / source_id,
        adapter,
        registry_path=DEFAULT_REGISTRY_PATH,
    )
    as_of = max(
        definition.effective_at
        for definition in runtime.loaded_registry.registry.definitions
    ) + timedelta(seconds=1)
    try:
        result = runtime.orchestrator.smoke_sources(
            ticker=ticker,
            source_ids=[source_id],
            as_of=as_of,
        )

        assert len(adapter.query_calls) == 1
        work = adapter.query_calls[0]
        if expected_encoding == "query":
            assert work.url.endswith("/queryCompanyStatementNew.do")
            assert work.params["productId"] == ticker
            assert work.params["reportType2"] == "DQBG"
            assert work.params["pageHelp.pageNo"] == 1
            assert work.params["pageHelp.pageSize"] == 100
            assert work.form_body is None
            assert work.json_body is None
        else:
            assert work.params == {}
            assert work.form_body is None
            assert work.json_body["stock"] == [ticker]
            assert work.json_body["channelCode"] == ["listedNotice_disc"]
            assert work.json_body["bigCategoryId"] == ["010301"]
            assert work.json_body["pageNum"] == 1
            assert work.json_body["pageSize"] == 50
        assert work.context["fetch_policy"] == "metadata_only"
        assert adapter.fetch_calls == []
        assert result.material_gap_count == 0
        assert result.checkpoint_advanced is False
    finally:
        runtime.close()
