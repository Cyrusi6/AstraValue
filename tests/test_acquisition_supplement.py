from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from analysis.acquisition.planner import AcquisitionPlanningError
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH, SourceRegistryLoader
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.acquisition.supplement import plan_supplement
from orchestrator_support import ScenarioAdapter, envelope, make_runtime


def failed_parent(tmp_path, ticker="600519"):
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url, status=504),
                              lambda *_: pytest.fail("must not parse failed HTTP"))
    runtime = make_runtime(tmp_path, adapter, registry_path=DEFAULT_REGISTRY_PATH)
    now = datetime.now(timezone.utc)
    plan = runtime.create_plan(runtime.build_profile(ticker), mode="incremental",
        run_kind="ad_hoc", as_of=now, start_at=now-timedelta(days=1))
    runtime.orchestrator.execute_run(plan.run.run_id)
    return runtime, plan, adapter


def test_default_source_roles_have_new_immutable_versions_and_zero_sse_io(tmp_path):
    runtime, parent, adapter = failed_parent(tmp_path)
    assert {w.source_definition_id for w in adapter.query_calls} == {"cninfo.disclosures"}
    assert not adapter.fetch_calls
    loaded = runtime.loaded_registry
    assert loaded.definition("cninfo.disclosures").collection_role == "primary"
    assert loaded.definition("cninfo.disclosures").version == "1.9.0"
    assert loaded.definition("sse.disclosures").version == "1.5.0"
    assert all(e.static_reason_code == "on_demand_supplement" for e in parent.coverage_entries
               if e.source_definition_id == "sse.disclosures")
    before = runtime.repository.list_coverage_resolutions(parent.run.run_id)
    entry = next(e for e in parent.coverage_entries if e.query_id == "cninfo.periodic_report")
    plan = plan_supplement(runtime, parent_run_id=parent.run.run_id,
        coverage_entry_id=entry.coverage_entry_id, source_definition_id="sse.disclosures")
    assert {p.query_id for p in plan.physical_query_plan_items} == {"sse.periodic_report"}
    assert plan.run.run_kind.value == "ad_hoc" and plan.run.mode.value == "reconcile"
    assert plan.run.parent_run_id == parent.run.run_id
    assert plan.run.reconcile_target["primary_barrier_resolution"] is False
    assert runtime.repository.list_coverage_resolutions(parent.run.run_id) == before
    assert all(p.time_start >= entry.time_start and p.time_end <= entry.time_end
               for p in plan.physical_query_plan_items)
    runtime.close()


@pytest.mark.parametrize("ticker,query", [("000001", "cninfo.periodic_report"),
                                         ("600519", "cninfo.prospectus")])
def test_supplement_rejects_wrong_market_or_unsupported_material(tmp_path, ticker, query):
    runtime, parent, _ = failed_parent(tmp_path, ticker)
    entry = next(e for e in parent.coverage_entries if e.query_id == query)
    with pytest.raises(AcquisitionPlanningError):
        plan_supplement(runtime, parent_run_id=parent.run.run_id,
            coverage_entry_id=entry.coverage_entry_id, source_definition_id="sse.disclosures")
    runtime.close()


def test_supplement_rejects_unfinalized_or_completed_parent(tmp_path, monkeypatch):
    runtime, parent, _ = failed_parent(tmp_path)
    entry = next(e for e in parent.coverage_entries if e.query_id == "cninfo.periodic_report")
    args = dict(parent_run_id=parent.run.run_id, coverage_entry_id=entry.coverage_entry_id,
                source_definition_id="sse.disclosures", persist=False)
    with monkeypatch.context() as m:
        m.setattr(runtime.repository, "list_run_events", lambda _: [])
        with pytest.raises(AcquisitionPlanningError, match="已终结"):
            plan_supplement(runtime, **args)
    monkeypatch.setattr(runtime.repository, "list_coverage_resolutions", lambda _: [
        SimpleNamespace(coverage_entry_id=entry.coverage_entry_id, material_gap_count=0)])
    with pytest.raises(AcquisitionPlanningError, match="不存在"):
        plan_supplement(runtime, **args)
    runtime.close()


def test_incremental_requires_primary_safe_checkpoint_but_not_supplement(tmp_path, monkeypatch):
    runtime = AcquisitionRuntime.create(tmp_path/"db", tmp_path/"data")
    profile = runtime.build_profile("600519")
    with pytest.raises(ValueError, match="cninfo.disclosures") as exc:
        runtime._incremental_start(profile)
    assert "sse.disclosures" not in str(exc.value)
    now = datetime.now(timezone.utc)
    calls = []
    def checkpoint(*args):
        calls.append(args[1])
        return SimpleNamespace(source_safe_through=SimpleNamespace(time_upper_bound=now))
    monkeypatch.setattr(runtime.repository, "latest_checkpoint", checkpoint)
    assert runtime._incremental_start(profile) < now
    assert calls == ["cninfo.disclosures"]
    runtime.close()


def test_v1_7_can_coexist_with_earlier_live_contracts_in_one_namespace(tmp_path):
    for version in ("1.4", "1.5", "1.6", "1.7"):
        with AcquisitionRuntime.create(tmp_path/"db", tmp_path/"data",
                registry_path=DEFAULT_REGISTRY_PATH.with_name(f"business_model_sources.v{version}.json")):
            pass
