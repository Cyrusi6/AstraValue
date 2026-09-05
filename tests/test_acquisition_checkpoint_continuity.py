from datetime import timedelta

import pytest

from analysis.acquisition.checkpoints import CheckpointEngine, PlanProgress
from analysis.acquisition.models import AcquisitionPlan, CheckpointPartition, CheckpointPosition, SnapshotIntegrityEvent, SourceCheckpoint, ValidatorAnchor
from analysis.acquisition.orchestrator import AcquisitionExecutionError
from orchestrator_support import NOW, ScenarioAdapter, discovery_result, envelope, make_runtime, no_data_adapter, resource, targeted_plan


def test_checkpoint_cannot_jump_an_unobserved_time_gap(acquisition_store):
    store = acquisition_store
    position = CheckpointPosition(time_upper_bound=store.now, canonical_resource_id="safe")
    previous = SourceCheckpoint(ticker=store.run.ticker,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        question_set_version=store.run.question_set_version, checkpoint_version=1,
        source_safe_through=position, overlap_days=14, finalized_lease_epoch=1,
        partitions=(CheckpointPartition(partition_key=store.plan_item.partition_key,
            execution_key=store.plan_item.execution_key, safe_through=position),))
    later = store.plan_item.model_copy(update={"time_start": store.now + timedelta(days=1),
        "time_end": store.now + timedelta(days=2)})
    result = CheckpointEngine().build(run=store.run,
        source_definition_id=later.source_definition_id, source_definition_version=later.source_definition_version,
        overlap_days=14, previous=previous, progress=[PlanProgress(later, True)])
    assert result.checkpoint.source_safe_through == position


def test_failed_overlap_blocks_historical_interval_from_advancing(acquisition_store):
    store = acquisition_store
    original = store.plan_item
    complete = original.model_copy(update={"time_start": store.now - timedelta(days=3), "time_end": store.now})
    failed = original.model_copy(update={"time_start": store.now - timedelta(days=1), "time_end": store.now})
    result = CheckpointEngine().build(run=store.run,
        source_definition_id=original.source_definition_id, source_definition_version=original.source_definition_version,
        overlap_days=14, progress=[PlanProgress(complete, True), PlanProgress(failed, False)])
    assert result.checkpoint.source_safe_through is None


def test_checkpoint_validator_uses_observation_time_not_interval_order(acquisition_store):
    store = acquisition_store
    early = store.plan_item.model_copy(update={"time_start": store.now - timedelta(days=2), "time_end": store.now - timedelta(days=1)})
    late = store.plan_item.model_copy(update={"time_start": store.now - timedelta(days=1), "time_end": store.now})
    fresh = ValidatorAnchor(canonical_resource_id="same", resource_url="https://example.test/a.pdf",
        snapshot_id="new", etag='"new"', observed_at=store.now)
    stale = fresh.model_copy(update={"snapshot_id": "old", "etag": '"old"', "observed_at": store.now - timedelta(days=1)})
    result = CheckpointEngine().build(run=store.run,
        source_definition_id=early.source_definition_id, source_definition_version=early.source_definition_version,
        overlap_days=14, progress=[PlanProgress(early, True, validator_anchors=(fresh,)), PlanProgress(late, True, validator_anchors=(stale,))])
    assert result.checkpoint.partitions[0].validator_anchors == (fresh,)


@pytest.mark.parametrize("suffix_state", ["valid", "damaged", "quarantined"])
def test_exact_reconcile_revalidates_completed_suffix_without_network_replay(tmp_path, suffix_state):
    keys = {}
    repairing = False
    def parsed(_, work):
        name = keys.get(work.execution_key)
        rows = () if name is None else (resource(name),)
        return discovery_result(work, resources=rows, declared_total=len(rows))
    def fetched(work):
        if work.resource.canonical_resource_id == "gap" and not repairing:
            return envelope(url=work.resource.url, status=502)
        return envelope(url=work.resource.url, body=b"%PDF-1.4\n" + work.resource.canonical_resource_id.encode() + b"\n%%EOF",
                        content_type="application/pdf", headers={"last-modified": "Wed, 02 Sep 2026 08:00:00 GMT"})
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url), parsed, fetched)
    with make_runtime(tmp_path, adapter) as runtime:
        profile = runtime.build_profile("600519", listing_date=(NOW - timedelta(days=800)).date())
        base = runtime.planner.plan(profile, mode="baseline", as_of=NOW, storage_namespace_id=runtime.namespace_id)
        items = tuple(p for p in base.physical_query_plan_items if p.query_id == "cninfo.periodic_report")
        assert len(items) == 3
        ids = {p.plan_item_id for p in items}
        links = tuple(link for link in base.coverage_links if link.plan_item_id in ids)
        cids = {link.coverage_entry_id for link in links}
        plan = AcquisitionPlan(run=base.run, physical_query_plan_items=items, coverage_links=links,
            coverage_entries=tuple(e for e in base.coverage_entries if e.coverage_entry_id in cids))
        runtime.orchestrator.persist_plan(plan)
        keys.update({items[1].execution_key: "gap", items[2].execution_key: "suffix"})
        baseline = runtime.orchestrator.execute_run(plan.run.run_id)
        assert baseline.material_gap_count > 0
        old_final = runtime.repository.list_run_events(plan.run.run_id)[-2:]
        checkpoint = runtime.repository.get_checkpoint(baseline.checkpoint_ids[0])
        assert checkpoint.source_safe_through.time_upper_bound == items[1].time_start
        if suffix_state != "valid":
            snapshot = runtime.repository.find_raw_resource_snapshot(resource_role="content", canonical_resource_id="suffix")
            if suffix_state == "damaged":
                (runtime.data_root / snapshot.archive_relative_path).write_bytes(b"damaged")
            else:
                runtime.repository.append_snapshot_integrity_event(SnapshotIntegrityEvent(
                    snapshot_id=snapshot.snapshot_id, status="quarantined", reason_code="review_rejected"))
        repairing = True
        before_queries, before_fetches = len(adapter.query_calls), len(adapter.fetch_calls)
        repair = runtime.plan_company_run("600519", mode="reconcile", parent_run_id=plan.run.run_id, as_of=NOW + timedelta(hours=1))
        outcome = runtime.orchestrator.execute_run(repair.run.run_id)
        assert outcome.material_gap_count == 0
        current = runtime.repository.get_checkpoint(outcome.checkpoint_ids[0])
        assert not current.unresolved_barrier_ids
        expected = plan.run.as_of if suffix_state == "valid" else items[1].time_end
        assert current.source_safe_through.time_upper_bound == expected
        assert all(w.execution_key != items[2].execution_key for w in adapter.query_calls[before_queries:])
        assert all(w.resource.canonical_resource_id != "suffix" for w in adapter.fetch_calls[before_fetches:])
        assert runtime.repository.list_run_events(plan.run.run_id)[-2:] == old_final
        final = next(e for e in runtime.repository.list_run_events(repair.run.run_id) if e.event_type.value == "finalized")
        assert (items[2].plan_item_id in final.metadata["checkpoint_reused_plan_item_ids"]) == (suffix_state == "valid")


@pytest.mark.parametrize("scope_field", ["ticker", "storage_namespace_id", "question_set_content_hash", "source_definition_refs"])
def test_checkpoint_history_rejects_another_scope_before_using_its_proofs(tmp_path, scope_field):
    adapter = no_data_adapter()
    with make_runtime(tmp_path, adapter) as runtime:
        plan = targeted_plan(runtime, mode="baseline")
        baseline = runtime.orchestrator.execute_run(plan.run.run_id)
        checkpoint = runtime.repository.get_checkpoint(baseline.checkpoint_ids[0])
        ref = plan.run.source_definition_refs[0]
        replacements = {
            "ticker": "000001",
            "storage_namespace_id": "another-namespace",
            "question_set_content_hash": "0" * 64,
            "source_definition_refs": (ref.model_copy(update={"content_hash": "0" * 64}),),
        }
        other = plan.run.model_copy(update={scope_field: replacements[scope_field]})
        calls = (len(adapter.query_calls), len(adapter.fetch_calls))
        with pytest.raises(AcquisitionExecutionError, match="checkpoint_history_scope_mismatch"):
            runtime.orchestrator._checkpoint_history_progress(
                other, checkpoint, ref.source_definition_id, str(ref.version), set())
        assert (len(adapter.query_calls), len(adapter.fetch_calls)) == calls


def test_sequential_reconciles_keep_the_other_gap_until_its_own_repair(tmp_path):
    keys, repaired = {}, set()
    def parsed(_, work):
        name = keys.get(work.execution_key)
        rows = () if name is None else (resource(name),)
        return discovery_result(work, resources=rows, declared_total=len(rows))
    def fetched(work):
        name = work.resource.canonical_resource_id
        if name.startswith("gap-") and name not in repaired:
            return envelope(url=work.resource.url, status=502)
        return envelope(url=work.resource.url, body=b"%PDF-1.4\n" + name.encode() + b"\n%%EOF",
                        content_type="application/pdf")
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url), parsed, fetched)
    with make_runtime(tmp_path, adapter) as runtime:
        profile = runtime.build_profile("600519", listing_date=(NOW - timedelta(days=1200)).date())
        base = runtime.planner.plan(profile, mode="baseline", as_of=NOW, storage_namespace_id=runtime.namespace_id)
        items = tuple(p for p in base.physical_query_plan_items if p.query_id == "cninfo.periodic_report")
        assert len(items) == 4
        ids = {p.plan_item_id for p in items}
        links = tuple(link for link in base.coverage_links if link.plan_item_id in ids)
        cids = {link.coverage_entry_id for link in links}
        plan = AcquisitionPlan(run=base.run, physical_query_plan_items=items, coverage_links=links,
            coverage_entries=tuple(e for e in base.coverage_entries if e.coverage_entry_id in cids))
        keys.update({items[1].execution_key: "gap-1", items[2].execution_key: "gap-2", items[3].execution_key: "suffix"})
        baseline = runtime.orchestrator.execute_plan(plan)
        assert baseline.material_gap_count == 2
        old_events = runtime.repository.list_run_events(plan.run.run_id)
        for index in (1, 2):
            repaired.add(f"gap-{index}")
            calls_before = len(adapter.query_calls)
            repair = runtime.plan_company_run("600519", mode="reconcile", parent_run_id=plan.run.run_id,
                                             as_of=NOW + timedelta(hours=index))
            assert repair.run.reconcile_target["canonical_resource_id"] == f"gap-{index}"
            outcome = runtime.orchestrator.execute_run(repair.run.run_id)
            assert outcome.material_gap_count == 2 - index
            checkpoint = runtime.repository.get_checkpoint(outcome.checkpoint_ids[0])
            remaining_resources = {runtime.repository.get_checkpoint_barrier(b)["canonical_resource_id"]
                                   for b in checkpoint.unresolved_barrier_ids}
            assert remaining_resources == ({"gap-2"} if index == 1 else set())
            expected = items[2].time_start if index == 1 else plan.run.as_of
            assert checkpoint.source_safe_through.time_upper_bound == expected
            assert all(work.execution_key != items[3].execution_key for work in adapter.query_calls[calls_before:])
        assert runtime.repository.list_run_events(plan.run.run_id) == old_events
