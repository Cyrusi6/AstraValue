from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import httpx
import pytest

from analysis.acquisition.adapters.base import DiscoveryResult, QueryWork
from analysis.acquisition.models import (
    AcquisitionAttemptEventType,
    AcquisitionMode,
    AcquisitionOutcome,
    AcquisitionPlan,
    AcquisitionRunEventType,
)
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH
from analysis.acquisition.repository import AcquisitionRepository
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.acquisition.orchestrator import AcquisitionOrchestrator

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


def _source_history_plan(runtime, *, query_id=None, days=800):
    profile = runtime.build_profile("600519", listing_date=(NOW - timedelta(days=days)).date())
    base = runtime.planner.plan(profile, mode="baseline", as_of=NOW,
                                storage_namespace_id=runtime.namespace_id)
    plans = tuple(p for p in base.physical_query_plan_items
                  if p.source_definition_id == "cninfo.disclosures"
                  and (query_id is None or p.query_id == query_id))
    ids = {p.plan_item_id for p in plans}
    links = tuple(link for link in base.coverage_links if link.plan_item_id in ids)
    coverage_ids = {link.coverage_entry_id for link in links}
    plan = AcquisitionPlan(run=base.run, physical_query_plan_items=plans, coverage_links=links,
                           coverage_entries=tuple(e for e in base.coverage_entries
                                                  if e.coverage_entry_id in coverage_ids))
    runtime.orchestrator.persist_plan(plan)
    return plan


def _terminals(runtime, run_id):
    return [(attempt, event)
            for attempt in runtime.repository.list_attempts(run_id=run_id)
            for event in runtime.repository.list_attempt_events(attempt.attempt_id)
            if event.event_type == AcquisitionAttemptEventType.OUTCOME_TERMINAL]


def _challenge_adapter(*, later_page=False):
    def response(work):
        if later_page and work.page == 2:
            return envelope(url=work.url, content_type="text/html",
                            headers={"x-tengine-error": "denied by bot"})
        return envelope(url=work.url)

    return ScenarioAdapter(response, lambda response, work: discovery_result(
        work, resources=tuple(resource(f"item-{n}") for n in range(3)),
        declared_total=6 if later_page else 3,
        page_count=2 if later_page else 1, terminal=not later_page,
    ), lambda work: envelope(url=work.query.url, content_type="text/html",
                             headers={"x-tengine-error": "denied by bot",
                                      "Set-Cookie": "private-secret"}))


@pytest.mark.parametrize("later_page", [False, True])
def test_source_halt_challenge_circuit_no_io_after_restricted_halted_barrier(tmp_path, later_page):
    adapter = _challenge_adapter(later_page=later_page)
    runtime = make_runtime(tmp_path / "halt", adapter)
    plan = _source_history_plan(runtime, query_id="cninfo.periodic_report")
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert len(adapter.query_calls) == (2 if later_page else 1)
    assert len(adapter.fetch_calls) == (0 if later_page else 1)
    assert result.coverage_accounted and result.result.value == "partial"
    assert not result.default_consume_eligible
    terms = _terminals(runtime, plan.run.run_id)
    opening = next(attempt for attempt, event in terms if event.reason_code == "upstream_bot_challenge")
    skips = [(attempt, event) for attempt, event in terms if event.reason_code == "source_access_halted"]
    assert len(skips) == len(plan.physical_query_plan_items) + (2 if later_page else 1)
    assert all(event.protocol_summary["halt_opening_attempt_id"] == opening.attempt_id
               and event.protocol_summary["io_performed"] is False for _, event in skips)
    assert len({event.protocol_summary["causal_group_id"] for _, event in skips}) == 1
    barriers = runtime.repository.list_checkpoint_barriers(unresolved_only=True)
    assert {row["opening_attempt_id"] for row in barriers} == {a.attempt_id for a, e in terms if e.outcome.value != "success"}
    checkpoint = runtime.repository.latest_checkpoint("600519", "cninfo.disclosures", "1.0.0", plan.run.question_set_version)
    assert checkpoint.source_safe_through is None
    assert all(runtime.repository.find_raw_resource_snapshot(
        resource_role="content", source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0", canonical_resource_id=f"item-{n}",
    ) is None for n in range(3))
    assert "private-secret" not in str(runtime.repository.list_resource_observations())


def test_halt_survives_lease_takeover_and_new_run_retries(tmp_path, monkeypatch):
    import time
    root = tmp_path / "takeover"
    adapter = _challenge_adapter()
    runtime = make_runtime(root, adapter, lease_ttl_seconds=1)
    plan = _source_history_plan(runtime, query_id="cninfo.periodic_report")
    terminal = runtime.orchestrator._terminal

    def crash_after_terminal(*args, **kwargs):
        fact = terminal(*args, **kwargs)
        if fact.reason_code == "upstream_bot_challenge":
            raise KeyboardInterrupt("fixture process loss")
        return fact

    monkeypatch.setattr(runtime.orchestrator, "_terminal", crash_after_terminal)
    monkeypatch.setattr(runtime.repository, "release_lease", lambda *a, **kw: None)
    with pytest.raises(KeyboardInterrupt):
        runtime.orchestrator.execute_run(plan.run.run_id)
    before = (len(adapter.query_calls), len(adapter.fetch_calls))
    old_epoch = runtime.repository.get_lease(plan.run.run_id).lease_epoch
    time.sleep(1.1)
    recovered = make_runtime(root, adapter)
    result = recovered.orchestrator.execute_run(plan.run.run_id)
    assert result.lease_epoch > old_epoch and result.coverage_accounted
    assert (len(adapter.query_calls), len(adapter.fetch_calls)) == before
    assert len([e for _, e in _terminals(recovered, plan.run.run_id)
                if e.reason_code == "upstream_bot_challenge"]) == 1
    new_plan = _source_history_plan(recovered, query_id="cninfo.periodic_report")
    recovered.orchestrator.execute_run(new_plan.run.run_id)
    assert len(adapter.query_calls) == before[0] + 1
    assert len(adapter.fetch_calls) == before[1] + 1


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


class BootstrapTimeoutRegistryProtocolAdapter(RegistryProtocolAdapter):
    def execute_query(self, work):
        self.query_calls.append(work)
        if work.query_family == "company_bootstrap":
            return envelope(
                url=work.url,
                status=504,
                content_type="text/html",
                body=b"<html>gateway timeout</html>",
            )
        raise AssertionError(
            "a query with an unresolved bootstrap binding must perform zero HTTP"
        )


def test_bootstrap_504_dependency_causal_group_for_all_history_plans(tmp_path):
    adapter = BootstrapTimeoutRegistryProtocolAdapter()
    runtime = make_runtime(tmp_path / "fanout", adapter, registry_path=DEFAULT_REGISTRY_PATH)
    plan = _source_history_plan(runtime, days=25 * 365)
    bootstrap = next(p for p in plan.physical_query_plan_items if p.query_family == "company_bootstrap")
    dependents = [p for p in plan.physical_query_plan_items if p.plan_item_id != bootstrap.plan_item_id]
    assert len(dependents) >= 100
    assert all(p.prerequisite_plan_item_ids == (bootstrap.plan_item_id,) for p in dependents)
    assert all(p.ordinal > bootstrap.ordinal for p in dependents)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert len(adapter.query_calls) == 1 and adapter.fetch_calls == []
    assert result.coverage_accounted and not result.default_consume_eligible
    terms = _terminals(runtime, plan.run.run_id)
    skips = [(a, e) for a, e in terms if e.reason_code == "dependency_unavailable"]
    assert len(skips) == len(dependents)
    assert all(e.outcome.value == "policy_skipped" and e.protocol_summary["io_performed"] is False
               for _, e in skips)
    assert len({e.protocol_summary["causal_group_id"] for _, e in skips}) == 1
    assert result.as_dict()["causal_groups"][0]["zero_io_attempt_count"] == len(dependents)
    assert runtime.orchestrator.execute_run(plan.run.run_id).causal_groups == result.causal_groups
    assert all(e.protocol_summary["prerequisites"][0]["reason_code"] == "http_504"
               for _, e in skips)
    barriers = runtime.repository.list_checkpoint_barriers(unresolved_only=True)
    assert len(barriers) == len(plan.physical_query_plan_items)
    checkpoint = runtime.repository.latest_checkpoint("600519", "cninfo.disclosures",
        bootstrap.source_definition_version, plan.run.question_set_version)
    assert checkpoint.source_safe_through is None


@pytest.mark.parametrize("binding_case", ["missing", "ambiguous", "empty", "no_data"])
def test_parameter_binding_invalid_distinct_from_dependency_unavailable(tmp_path, binding_case):
    class BindingAdapter(RegistryProtocolAdapter):
        def parse_retained_discovery(self, snapshot_id, work):
            parsed = super().parse_retained_discovery(snapshot_id, work)
            if work.query_family != "company_bootstrap":
                return parsed
            row = parsed.resources[0]
            rows = (replace(row, metadata={"ticker": "600519"}),)
            if binding_case == "empty":
                rows = (replace(row, metadata={"ticker": "600519", "wire_stock": ""}),)
            elif binding_case == "ambiguous":
                rows = (row, replace(row, canonical_resource_id="second-company",
                    row_locator="second", row_hash="a" * 64,
                    metadata={"ticker": "600519", "wire_stock": "600519,another-org"}))
            elif binding_case == "no_data":
                rows = ()
            return discovery_result(work, resources=rows, declared_total=len(rows))

    adapter = BindingAdapter()
    runtime = make_runtime(tmp_path / binding_case, adapter, registry_path=DEFAULT_REGISTRY_PATH)
    result = runtime.orchestrator.smoke_sources(ticker="600519", source_ids=["cninfo.disclosures"], as_of=NOW)
    assert len(adapter.query_calls) == 1
    dependent = next(e for a, e in _terminals(runtime, result.run_id)
                     if a.query_id == "cninfo.periodic_report")
    assert dependent.reason_code == ("dependency_unavailable" if binding_case == "no_data"
                                     else "parameter_binding_invalid")
    assert dependent.protocol_summary["prerequisites"][0]["proof_ids"]


def test_dependency_unavailable_without_terminal_proof_zero_downstream_io(tmp_path):
    adapter = RegistryProtocolAdapter()
    runtime = make_runtime(tmp_path / "missing-proof", adapter, registry_path=DEFAULT_REGISTRY_PATH)
    plan = targeted_plan(runtime)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert result.coverage_accounted and adapter.query_calls == []
    assert _terminals(runtime, plan.run.run_id)[0][1].reason_code == "dependency_unavailable"


def test_dependency_causal_group_reconcile_includes_only_target_and_prerequisite(tmp_path):
    class MissingBinding(RegistryProtocolAdapter):
        def parse_retained_discovery(self, snapshot_id, work):
            result = super().parse_retained_discovery(snapshot_id, work)
            if work.query_family == "company_bootstrap":
                return replace(result, resources=(replace(result.resources[0], metadata={"ticker": "600519"}),))
            return result
    adapter = MissingBinding()
    runtime = make_runtime(tmp_path / "prerequisite-target", adapter, registry_path=DEFAULT_REGISTRY_PATH)
    parent = _source_history_plan(runtime, days=30)
    runtime.orchestrator.execute_run(parent.run.run_id)
    repair = runtime.plan_company_run("600519", mode="reconcile", parent_run_id=parent.run.run_id, as_of=NOW)
    target_query = repair.run.reconcile_target["query_id"]
    assert target_query != "cninfo.company_bootstrap"
    assert {p.query_id for p in repair.physical_query_plan_items} == {target_query, "cninfo.company_bootstrap"}
    by_id = {p.plan_item_id: p for p in repair.physical_query_plan_items}
    assert all(by_id[dependency].ordinal < p.ordinal
               for p in repair.physical_query_plan_items for dependency in p.prerequisite_plan_item_ids)


def test_dependency_causal_group_graph_rejects_cycle(tmp_path):
    from analysis.acquisition.dependencies import order_dependency_plans
    runtime = make_runtime(tmp_path / "cycle", RegistryProtocolAdapter(), registry_path=DEFAULT_REGISTRY_PATH)
    plan = _source_history_plan(runtime, days=30)
    definition = runtime.source_definition("cninfo.disclosures")
    bootstrap, consumer = definition.queries[:2]
    binding = next(iter(consumer.parameter_bindings.values())).model_copy(update={"source_query_id": consumer.query_id})
    cyclic = definition.model_copy(update={"queries": (
        bootstrap.model_copy(update={"parameter_bindings": {"fixture": binding}}),
        *definition.queries[1:],
    )})
    with pytest.raises(ValueError, match="环"):
        order_dependency_plans(plan.physical_query_plan_items, (cyclic,))


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


def test_material_gap_count_keeps_distinct_time_slices_in_one_partition(
    tmp_path,
) -> None:
    adapter = ScenarioAdapter(
        lambda work: envelope(url=work.url, status=503),
        lambda response, work: discovery_result(work),
    )
    runtime = make_runtime(
        tmp_path / "gap-time-slices",
        adapter,
        registry_path=DEFAULT_REGISTRY_PATH,
    )
    as_of = max(
        definition.effective_at
        for definition in runtime.loaded_registry.registry.definitions
    ) + timedelta(seconds=1)
    profile = runtime.build_profile(
        "600519",
        company_name="贵州茅台",
        listing_date=(as_of - timedelta(days=800)).date(),
    )
    base = runtime.planner.plan(
        profile,
        mode=AcquisitionMode.BASELINE,
        as_of=as_of,
        storage_namespace_id=runtime.namespace_id,
    )
    plans = tuple(
        item
        for item in base.physical_query_plan_items
        if item.source_definition_id == "sse.disclosures"
        and item.query_id == "sse.periodic_report"
    )
    assert len(plans) >= 3
    plan_ids = {item.plan_item_id for item in plans}
    links = tuple(item for item in base.coverage_links if item.plan_item_id in plan_ids)
    coverage_ids = {item.coverage_entry_id for item in links}
    coverage = tuple(
        item for item in base.coverage_entries if item.coverage_entry_id in coverage_ids
    )
    source_refs = tuple(
        item
        for item in base.run.source_definition_refs
        if item.source_definition_id == "sse.disclosures"
    )
    plan = AcquisitionPlan(
        run=base.run.model_copy(update={"source_definition_refs": source_refs}),
        physical_query_plan_items=plans,
        coverage_entries=coverage,
        coverage_links=links,
    )
    runtime.orchestrator.persist_plan(plan)

    try:
        result = runtime.orchestrator.execute_run(plan.run.run_id)

        assert result.material_gap_count == len(plans)
        assert len(
            runtime.repository.list_checkpoint_barriers(unresolved_only=True)
        ) == len(plans)
    finally:
        runtime.close()


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


@pytest.mark.parametrize("case", ["empty", "conflicting_count", "challenge"])
def test_cninfo_nullable_v2_runs_through_retention_and_outcome_pipeline(tmp_path, case):
    calls = []

    class FixtureTransport:
        def request(self, work):
            calls.append(work)
            if work.query_family == "company_bootstrap":
                payload = {"stockList": [{"code": "600519", "orgId": "gssh0600519",
                                          "zwjc": "Synthetic Company"}]}
                headers = {}
            else:
                assert work.form_body["stock"] == "600519,gssh0600519"
                payload = {"announcements": None, "totalAnnouncement": 0,
                           "hasMore": False, "totalRecordNum": 0}
                if case == "conflicting_count":
                    payload["totalRecordNum"] = 1
                headers = {"x-tengine-error": "denied by bot"} if case == "challenge" else {}
            return envelope(url=work.url, body=json.dumps(payload).encode(), headers=headers)

        def close(self):
            pass

    runtime = AcquisitionRuntime.create(tmp_path / "analysis.db", tmp_path / "data",
                                        workspace_root=tmp_path)
    runtime.orchestrator = AcquisitionOrchestrator(
        runtime, transport_factory=lambda _definition: FixtureTransport())
    try:
        result = runtime.orchestrator.smoke_sources(ticker="600519", source_ids=["cninfo.disclosures"])
        assert len(calls) == 2
        assert calls[1].parser_schema_version == "2"
        assert calls[1].context["schema_id"] == "cninfo.announcements"
        run = runtime.repository.get_run(result.run_id)
        assert run.http_route_policy == "direct-v1"
        attempts = runtime.repository.list_attempts(run_id=run.run_id)
        periodic = next(a for a in attempts if a.query_id == "cninfo.periodic_report")
        proofs = runtime.repository.list_discovery_proofs(periodic.attempt_id)
        if case == "empty":
            assert result.outcome_counts == {"success": 1, "no_data": 1}
            assert result.material_gap_count == 0
            assert len(proofs) == 1 and proofs[0].terminal
            assert proofs[0].declared_total == proofs[0].normalized_row_count == 0
            assert proofs[0].replayable and proofs[0].schema_version == "2"
        else:
            expected = "restricted" if case == "challenge" else "parse_failed"
            assert result.outcome_counts == {"success": 1, expected: 1}
            assert result.material_gap_count > 0
            assert proofs == []
        assert result.checkpoint_advanced is False
    finally:
        runtime.close()


def test_cninfo_bootstrap_504_dependency_unavailable_zero_downstream_io(
    tmp_path,
) -> None:
    adapter = BootstrapTimeoutRegistryProtocolAdapter()
    runtime = make_runtime(
        tmp_path / "cninfo-bootstrap-timeout",
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
            "cninfo.company_bootstrap"
        ]
        plans = {
            item.plan_item_id: item
            for item in runtime.repository.list_physical_query_plan_items(result.run_id)
        }
        attempts = runtime.repository.list_attempts(run_id=result.run_id)
        assert {plans[item.physical_query_plan_item_id].query_id for item in attempts} == {
            "cninfo.company_bootstrap",
            "cninfo.periodic_report",
        }
        terminal_by_query = {}
        for attempt in attempts:
            events = runtime.repository.list_attempt_events(attempt.attempt_id)
            terminal = [
                event
                for event in events
                if event.event_type == AcquisitionAttemptEventType.OUTCOME_TERMINAL
            ]
            assert len(terminal) == 1
            terminal_by_query[plans[attempt.physical_query_plan_item_id].query_id] = (
                terminal[0].outcome,
                terminal[0].reason_code,
            )

        assert terminal_by_query["cninfo.company_bootstrap"] == (
            AcquisitionOutcome.TIMEOUT,
            "http_504",
        )
        assert terminal_by_query["cninfo.periodic_report"] == (
            AcquisitionOutcome.POLICY_SKIPPED,
            "dependency_unavailable",
        )
        assert result.result.value == "partial"
        assert result.coverage_accounted is True
        assert result.material_gap_count == 2
        assert result.default_consume_eligible is False
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
