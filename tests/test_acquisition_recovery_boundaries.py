from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from orchestrator_support import NOW, discovery_result, envelope, make_runtime, no_data_adapter, targeted_plan


def repair_parent(root, *, legacy=False):
    adapter = no_data_adapter()
    runtime = make_runtime(root, adapter)
    original = targeted_plan(runtime)
    runtime.orchestrator.execute_run(original.run.run_id)
    repair = runtime.plan_company_run("600519", mode="reconcile", as_of=NOW,
        parent_run_id=original.run.run_id, persist=False)
    if legacy:
        target = {key: value for key, value in repair.run.reconcile_target.items()
                  if not key.startswith("range_")}
        repair = repair.model_copy(update={"run": repair.run.model_copy(update={"reconcile_target": target})})
    overlap = min(repair.physical_query_plan_items, key=lambda p: p.time_start)
    adapter.discovery_response = lambda work: envelope(url=work.url,
        status=502 if work.execution_key == overlap.execution_key else 200)
    runtime.orchestrator.execute_plan(repair)
    return runtime, adapter, original, repair, overlap


@pytest.mark.parametrize("legacy", [False, True])
def test_repeated_failed_overlap_stays_bounded_and_preserves_history(tmp_path, legacy):
    runtime, adapter, original, repair, overlap = repair_parent(tmp_path, legacy=legacy)
    old_events = tuple(runtime.repository.list_run_events(repair.run.run_id))
    old_attempts = tuple(runtime.repository.list_attempts(run_id=repair.run.run_id))
    floor = overlap.time_start
    assert floor == original.physical_query_plan_items[0].time_start - timedelta(days=14)
    parent = repair
    for _ in range(3):
        child = runtime.plan_company_run("600519", mode="reconcile", as_of=NOW, parent_run_id=parent.run.run_id)
        target = child.run.reconcile_target
        assert datetime.fromisoformat(target["range_floor"]) == floor
        assert target["range_policy_version"] == "bounded-parent-range-v1"
        assert target["range_floor_source"] == f"parent_effective_range:{parent.run.run_id}"
        assert target["overlap_days"] == 14
        assert len(child.physical_query_plan_items) == 1
        assert child.physical_query_plan_items[0].execution_key == overlap.execution_key
        runtime.orchestrator.execute_run(child.run.run_id)
        parent = child
    adapter.discovery_response = lambda work: envelope(url=work.url)
    recovered = runtime.plan_company_run("600519", mode="reconcile", as_of=NOW, parent_run_id=parent.run.run_id)
    result = runtime.orchestrator.execute_run(recovered.run.run_id)
    assert result.material_gap_count == 0
    assert not runtime.repository.list_checkpoint_barriers(unresolved_only=True)
    assert tuple(runtime.repository.list_run_events(repair.run.run_id)) == old_events
    assert tuple(runtime.repository.list_attempts(run_id=repair.run.run_id)) == old_attempts
    import runpy
    from pathlib import Path
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/validate_acquisition_consistency.py"))["validate"](
        runtime.db_path, runtime.data_root)


@pytest.mark.parametrize("case", [
    "missing_target", "missing_range", "naive", "inverted", "outside_as_of", "unknown_version",
    "wrong_floor", "wrong_parent", "wrong_origin", "wrong_source", "wrong_query", "truncated_prefix",
    "wrong_partition", "wrong_pagination", "wrong_plan_source", "missing_suffix",
    "missing_floor_source", "wrong_floor_source", "partial_legacy_policy",
])
def test_invalid_parent_rejected_before_run_plan_or_io(tmp_path, monkeypatch, case):
    runtime, adapter, original, repair, overlap = repair_parent(tmp_path)
    target = deepcopy(repair.run.reconcile_target)
    range_ = target["effective_range"]
    if case == "missing_target":
        target = None
    elif case == "missing_range":
        del target["effective_range"]
    elif case == "naive":
        range_["time_start"] = "2020-01-01T00:00:00"
    elif case == "inverted":
        range_["time_start"] = range_["time_end"]
    elif case == "outside_as_of":
        range_["time_end"] = (NOW + timedelta(days=1)).isoformat()
    elif case == "unknown_version":
        target["range_policy_version"] = "future-v2"
    elif case == "wrong_floor":
        target["range_floor"] = range_["time_end"]
    elif case == "missing_floor_source":
        del target["range_floor_source"]
    elif case == "wrong_floor_source":
        target["range_floor_source"] = "fabricated"
    elif case == "partial_legacy_policy":
        del target["range_policy_version"]
        target["range_floor"] = range_["time_end"]
    elif case == "wrong_parent":
        target["resolved_parent_run_id"] = "another-run"
    elif case == "wrong_origin":
        target["plan_item_id"] = overlap.plan_item_id
    elif case == "wrong_source":
        target["source_definition_version"] = "99.0.0"
    elif case == "wrong_query":
        target["query_id"] = "cninfo.ipo"
    elif case == "truncated_prefix":
        range_["time_start"] = (overlap.time_start + timedelta(days=1)).isoformat()
        target["range_floor"] = range_["time_start"]
    get_run = runtime.repository.get_run
    monkeypatch.setattr(runtime.repository, "get_run", lambda rid: (
        repair.run.model_copy(update={"reconcile_target": target}) if rid == repair.run.run_id else get_run(rid)))
    list_plans = runtime.repository.list_physical_query_plan_items
    if case in {"wrong_partition", "wrong_pagination", "wrong_plan_source", "missing_suffix"}:
        fields = {"wrong_partition": {"partition_key": "wrong"},
                  "wrong_pagination": {"pagination_fingerprint": "bad"},
                  "wrong_plan_source": {"source_definition_version": "99.0.0"}}
        def changed_plans(rid):
            items = list_plans(rid)
            if rid != repair.run.run_id:
                return items
            if case == "missing_suffix":
                # Keep the graph intact but leave an unexplained time gap.
                return [p.model_copy(update={"time_end": p.time_end - timedelta(hours=1)})
                        if p.plan_item_id == overlap.plan_item_id else p for p in items]
            return [p.model_copy(update=fields[case]) for p in items]
        monkeypatch.setattr(runtime.repository, "list_physical_query_plan_items", changed_plans)
    before = (len(runtime.repository.list_runs()), len(adapter.query_calls), len(adapter.fetch_calls))
    with pytest.raises(ValueError, match="reconcile父恢复范围"):
        runtime.plan_company_run("600519", mode="reconcile", as_of=NOW, parent_run_id=repair.run.run_id)
    assert (len(runtime.repository.list_runs()), len(adapter.query_calls), len(adapter.fetch_calls)) == before


def test_existing_frozen_plan_executes_without_recomputing_target(tmp_path, monkeypatch):
    runtime, adapter, original, repair, overlap = repair_parent(tmp_path)
    child = runtime.plan_company_run("600519", mode="reconcile", as_of=NOW, parent_run_id=repair.run.run_id)
    frozen = runtime.repository.get_run(child.run.run_id).model_dump_json()
    adapter.discovery_response = lambda work: envelope(url=work.url)
    monkeypatch.setattr("analysis.acquisition.reconcile.select_reconcile_target",
                        lambda *args, **kwargs: pytest.fail("frozen execution must not replan"))
    assert runtime.orchestrator.execute_run(child.run.run_id).material_gap_count == 0
    assert runtime.repository.get_run(child.run.run_id).model_dump_json() == frozen


def test_bootstrap_barrier_of_business_target_uses_dependency_closure(tmp_path):
    from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH
    from test_acquisition_orchestrator import RegistryProtocolAdapter, _source_history_plan

    class Adapter(RegistryProtocolAdapter):
        fail_bootstrap = False

        def execute_query(self, work):
            self.query_calls.append(work)
            return envelope(url=work.url, status=504 if self.fail_bootstrap else 200)

        def parse_retained_discovery(self, snapshot_id, work):
            if work.query_family == "company_bootstrap":
                return super().parse_retained_discovery(snapshot_id, work)
            return discovery_result(work)

    adapter = Adapter()
    runtime = make_runtime(tmp_path, adapter, registry_path=DEFAULT_REGISTRY_PATH)
    # A failed business query in an otherwise successful parent selects it first.
    failed = _source_history_plan(runtime, days=1)
    business = next(p for p in failed.physical_query_plan_items if p.query_id == "cninfo.business_announcement")
    adapter.execute_query = lambda work: envelope(url=work.url, status=502 if work.execution_key == business.execution_key else 200)
    runtime.orchestrator.execute_run(failed.run.run_id)
    parent = runtime.plan_company_run("600519", mode="reconcile", as_of=NOW, parent_run_id=failed.run.run_id)
    assert parent.run.reconcile_target["query_id"] == business.query_id
    adapter.execute_query = Adapter.execute_query.__get__(adapter, Adapter)
    adapter.fail_bootstrap = True
    runtime.orchestrator.execute_run(parent.run.run_id)
    before_calls = len(adapter.query_calls)
    child = runtime.plan_company_run("600519", mode="reconcile", as_of=NOW, parent_run_id=parent.run.run_id)
    assert child.run.reconcile_target["query_id"] == "cninfo.company_bootstrap"
    assert child.run.reconcile_target["effective_range"] == parent.run.reconcile_target["effective_range"]
    assert len(adapter.query_calls) == before_calls
    assert {p.query_id for p in child.physical_query_plan_items} == {"cninfo.company_bootstrap"}
