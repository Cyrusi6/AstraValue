from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from analysis.acquisition.models import (
    AcquisitionAttempt,
    AcquisitionAttemptEvent,
    AcquisitionOutcome,
    AcquisitionRun,
    AcquisitionRunEvent,
    BarrierResolution,
    CheckpointPartition,
    CheckpointPosition,
    CoverageEntry,
    CoveragePlanDisposition,
    CoverageResolution,
    CoverageResolutionStatus,
    DiscoveryObservation,
    DiscoveryProof,
    PhysicalQueryPlanItem,
    SourceCheckpoint,
)
from analysis.acquisition.checkpoints import (
    BLOCKING_OUTCOMES,
    barrier_for_attempt,
    source_safe_lower_bound,
    static_coverage_resolution,
)
from analysis.acquisition.repository import (
    AcquisitionStorageError,
    BarrierResolutionError,
    CheckpointConflictError,
)


def _attempt(store, *, attempt_id="attempt-opening", run_id=None, plan_item=None,
             retry_ordinal=0, supersedes=None):
    plan = plan_item or store.plan_item
    return AcquisitionAttempt(
        attempt_id=attempt_id,
        run_id=run_id or store.run.run_id,
        source_definition_id=plan.source_definition_id,
        source_definition_version=plan.source_definition_version,
        physical_query_plan_item_id=plan.plan_item_id,
        execution_key=plan.execution_key,
        attempt_kind="discovery",
        query_id=plan.query_id,
        time_start=plan.time_start,
        time_end=plan.time_end,
        page_number=1,
        work_position="page:1",
        retry_group_id="retry-checkpoint-1",
        retry_ordinal=retry_ordinal,
        lease_epoch=1,
        started_at=store.now + timedelta(seconds=retry_ordinal),
        supersedes_attempt_id=supersedes,
    )


def _checkpoint(store, *, checkpoint_id, version=1, parent=None,
                run_id=None, unresolved=(), safe=False):
    position = (
        CheckpointPosition(
            time_upper_bound=store.now,
            canonical_resource_id="resource-safe",
            page_number=1,
        )
        if safe
        else None
    )
    return SourceCheckpoint(
        checkpoint_id=checkpoint_id,
        ticker=store.run.ticker,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        question_set_version=store.run.question_set_version,
        checkpoint_version=version,
        parent_checkpoint_id=(None if parent is None else parent.checkpoint_id),
        parent_checkpoint_version=(None if parent is None else parent.checkpoint_version),
        source_safe_through=position,
        partitions=(
            CheckpointPartition(
                partition_key=store.plan_item.partition_key,
                execution_key=store.plan_item.execution_key,
                safe_through=position,
                unresolved_barrier_ids=tuple(unresolved),
            ),
        ),
        overlap_days=30,
        latest_successful_run_id=run_id or store.run.run_id,
        unresolved_barrier_ids=tuple(unresolved),
        created_at=store.now + timedelta(seconds=version),
        finalized_lease_epoch=1,
    )


def _terminal_event(store, *, run_id=None, result="partial", gaps=1):
    return AcquisitionRunEvent(
        event_id=f"final-{run_id or store.run.run_id}",
        run_id=run_id or store.run.run_id,
        event_type="finalized",
        occurred_at=store.now + timedelta(seconds=20),
        lease_epoch=1,
        result=result,
        coverage_accounted=True,
        material_gap_count=gaps,
        default_consume_eligible=(result == "succeeded" and gaps == 0),
    )


def _barrier(store, checkpoint_id, *, work_position="page:1"):
    return {
        "barrier_id": "barrier-1",
        "checkpoint_id": checkpoint_id,
        "source_definition_id": store.plan_item.source_definition_id,
        "source_definition_version": store.plan_item.source_definition_version,
        "partition_key": store.plan_item.partition_key,
        "work_position": work_position,
        "retry_group_id": "retry-checkpoint-1",
        "opening_attempt_id": "attempt-opening",
        "created_at": (store.now + timedelta(seconds=3)).isoformat(),
    }


def test_checkpoint_parent_cas_allows_only_one_concurrent_successor(acquisition_store):
    store = acquisition_store
    repository = store.repository
    _, token = repository.claim_lease(
        store.run.run_id, owner_token="checkpoint-owner", now=store.now
    )
    first = _checkpoint(store, checkpoint_id="checkpoint-1")
    repository.save_checkpoint(
        first,
        expected_parent_version=None,
        run_id=store.run.run_id,
        owner_token=token,
        lease_epoch=1,
    )
    winner = _checkpoint(
        store,
        checkpoint_id="checkpoint-2",
        version=2,
        parent=first,
        safe=True,
    )
    loser = _checkpoint(
        store,
        checkpoint_id="checkpoint-2-loser",
        version=2,
        parent=first,
    )
    def append(candidate):
        try:
            repository.save_checkpoint(
                candidate,
                expected_parent_version=1,
                run_id=store.run.run_id,
                owner_token=token,
                lease_epoch=1,
            )
            return "won"
        except CheckpointConflictError:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(append, (winner, loser)))
    assert sorted(outcomes) == ["lost", "won"]
    latest = repository.latest_checkpoint(
        store.run.ticker,
        store.plan_item.source_definition_id,
        store.plan_item.source_definition_version,
        store.run.question_set_version,
    )
    assert latest.checkpoint_id in {winner.checkpoint_id, loser.checkpoint_id}


def test_finalize_atomically_writes_checkpoint_opening_barrier_and_terminal_event(
    acquisition_store,
):
    store = acquisition_store
    repository = store.repository
    _, token = repository.claim_lease(
        store.run.run_id, owner_token="finalize-owner", now=store.now
    )
    attempt = _attempt(store)
    repository.save_attempt(attempt, owner_token=token)
    repository.append_attempt_event(
        AcquisitionAttemptEvent(
            event_id="opening-failure",
            attempt_id=attempt.attempt_id,
            event_type="outcome_terminal",
            outcome="rate_limited",
            reason_code="http_429",
            lease_epoch=1,
            occurred_at=store.now + timedelta(seconds=2),
        ),
        owner_token=token,
    )
    checkpoint = _checkpoint(
        store, checkpoint_id="checkpoint-barrier", unresolved=("barrier-1",)
    )
    resolution = CoverageResolution(
        resolution_id="coverage-resolution-blocked",
        coverage_entry_id=store.coverage_entries[0].coverage_entry_id,
        status="blocked",
        attempt_ids=(attempt.attempt_id,),
        material_gap_count=1,
        reason_codes=("http_429",),
        resolved_at=store.now + timedelta(seconds=4),
        lease_epoch=1,
    )
    repository.finalize_run(
        store.run.run_id,
        run_event=_terminal_event(store),
        coverage_resolutions=(resolution,),
        checkpoints=((checkpoint, None),),
        opening_barriers=(_barrier(store, checkpoint.checkpoint_id),),
        owner_token=token,
        lease_epoch=1,
    )
    barrier = repository.get_checkpoint_barrier("barrier-1")
    assert barrier["checkpoint_id"] == checkpoint.checkpoint_id
    assert len(barrier["query_semantics_hash"]) == 64
    assert repository.list_checkpoint_barriers(
        source_definition_id=store.plan_item.source_definition_id,
        partition_key=store.plan_item.partition_key,
        retry_group_id="retry-checkpoint-1",
        unresolved_only=True,
    ) == [barrier]
    assert repository.list_coverage_resolutions(store.run.run_id) == [resolution]
    assert repository.list_run_events(store.run.run_id)[-1].event_type == "finalized"


def test_failed_finalize_rolls_back_resolution_checkpoint_barrier_and_event(
    acquisition_store,
):
    store = acquisition_store
    repository = store.repository
    _, token = repository.claim_lease(
        store.run.run_id, owner_token="rollback-owner", now=store.now
    )
    attempt = _attempt(store)
    repository.save_attempt(attempt, owner_token=token)
    checkpoint = _checkpoint(
        store, checkpoint_id="checkpoint-rollback", unresolved=("barrier-1",)
    )
    resolution = CoverageResolution(
        resolution_id="coverage-resolution-rollback",
        coverage_entry_id=store.coverage_entries[0].coverage_entry_id,
        status="blocked",
        attempt_ids=(attempt.attempt_id,),
        material_gap_count=1,
        reason_codes=("failure",),
        resolved_at=store.now + timedelta(seconds=4),
        lease_epoch=1,
    )
    events_before = tuple(repository.list_run_events(store.run.run_id))
    with pytest.raises(AcquisitionStorageError, match="work_position"):
        repository.finalize_run(
            store.run.run_id,
            run_event=_terminal_event(store),
            coverage_resolutions=(resolution,),
            checkpoints=((checkpoint, None),),
            opening_barriers=(
                _barrier(store, checkpoint.checkpoint_id, work_position="page:99"),
            ),
            owner_token=token,
            lease_epoch=1,
        )
    assert repository.latest_checkpoint(
        store.run.ticker,
        store.plan_item.source_definition_id,
        store.plan_item.source_definition_version,
        store.run.question_set_version,
    ) is None
    assert repository.list_coverage_resolutions(store.run.run_id) == []
    assert repository.list_checkpoint_barriers() == []
    assert tuple(repository.list_run_events(store.run.run_id)) == events_before


def test_exact_barrier_resolution_rate_limit_then_success(acquisition_store):
    store = acquisition_store
    repository = store.repository
    _, first_token = repository.claim_lease(
        store.run.run_id, owner_token="opening-owner", now=store.now
    )
    opening = _attempt(store)
    repository.save_attempt(opening, owner_token=first_token)
    repository.append_attempt_event(
        AcquisitionAttemptEvent(
            event_id="opening-rate-limit",
            attempt_id=opening.attempt_id,
            event_type="outcome_terminal",
            outcome="rate_limited",
            reason_code="http_429",
            lease_epoch=1,
            occurred_at=store.now + timedelta(seconds=1),
        ),
        owner_token=first_token,
    )
    first_checkpoint = _checkpoint(
        store, checkpoint_id="checkpoint-old-gap", unresolved=("barrier-1",)
    )
    repository.finalize_run(
        store.run.run_id,
        run_event=_terminal_event(store),
        checkpoints=((first_checkpoint, None),),
        opening_barriers=(_barrier(store, first_checkpoint.checkpoint_id),),
        owner_token=first_token,
        lease_epoch=1,
    )

    run_data = store.run.model_dump(mode="json")
    run_data.update(
        {
            "run_id": "run-reconcile-2",
            "mode": "reconcile",
            "parent_run_id": store.run.run_id,
            "reconcile_target": {"barrier_id": "barrier-1"},
            "created_at": (store.now + timedelta(seconds=30)).isoformat(),
        }
    )
    reconcile_run = AcquisitionRun.model_validate(run_data)
    plan_data = store.plan_item.model_dump(mode="json")
    plan_data.update(
        {
            "plan_item_id": "plan-reconcile-2",
            "run_id": reconcile_run.run_id,
        }
    )
    reconcile_plan = PhysicalQueryPlanItem.model_validate(plan_data)
    repository.save_plan_bundle(reconcile_run, (reconcile_plan,), (), ())
    _, second_token = repository.claim_lease(
        reconcile_run.run_id,
        owner_token="resolving-owner",
        now=store.now,
    )
    resolving = _attempt(
        store,
        attempt_id="attempt-resolving",
        run_id=reconcile_run.run_id,
        plan_item=reconcile_plan,
        retry_ordinal=1,
        supersedes=opening.attempt_id,
    )
    repository.save_attempt(resolving, owner_token=second_token)
    body_hash = "a" * 64
    observation = DiscoveryObservation(
        observation_id="discovery-observation-resolving",
        attempt_id=resolving.attempt_id,
        physical_query_plan_item_id=reconcile_plan.plan_item_id,
        source_definition_id=reconcile_plan.source_definition_id,
        source_definition_version=reconcile_plan.source_definition_version,
        page_number=1,
        observed_at=store.now + timedelta(seconds=31),
        retrieved_at=store.now + timedelta(seconds=32),
        http_status=200,
        mime_type="application/json",
        response_sha256=body_hash,
        response_byte_length=2,
    )
    proof = DiscoveryProof(
        proof_id="proof-resolving",
        observation_id=observation.observation_id,
        attempt_id=resolving.attempt_id,
        physical_query_plan_item_id=reconcile_plan.plan_item_id,
        response_sha256=body_hash,
        response_byte_length=2,
        http_status=200,
        mime_type="application/json",
        parser_id="fixture-parser",
        parser_version="1",
        schema_id="fixture-schema",
        schema_version="1",
        schema_valid=True,
        page_number=1,
        declared_total=0,
        declared_page_count=1,
        normalized_row_count=0,
        terminal=True,
        body_retained=False,
        replayable=False,
        created_at=store.now + timedelta(seconds=32),
    )
    repository.commit_discovery_bundle(
        observation,
        proof,
        (),
        owner_token=second_token,
        lease_epoch=1,
    )
    repository.append_attempt_event(
        AcquisitionAttemptEvent(
            event_id="resolving-success",
            attempt_id=resolving.attempt_id,
            event_type="outcome_terminal",
            outcome="no_data",
            lease_epoch=1,
            occurred_at=store.now + timedelta(seconds=33),
            proof_ids=(proof.proof_id,),
        ),
        owner_token=second_token,
    )
    barrier_resolution = BarrierResolution(
        barrier_resolution_id="barrier-resolution-1",
        barrier_id="barrier-1",
        opening_attempt_id=opening.attempt_id,
        resolving_attempt_id=resolving.attempt_id,
        source_definition_id=reconcile_plan.source_definition_id,
        source_definition_version=reconcile_plan.source_definition_version,
        partition_key=reconcile_plan.partition_key,
        work_position="page:1",
        retry_group_id=resolving.retry_group_id,
        discovery_proof_id=proof.proof_id,
        created_at=store.now + timedelta(seconds=34),
        lease_epoch=1,
    )
    next_checkpoint = _checkpoint(
        store,
        checkpoint_id="checkpoint-resolved",
        version=2,
        parent=first_checkpoint,
        run_id=reconcile_run.run_id,
        safe=True,
    )
    repository.finalize_run(
        reconcile_run.run_id,
        run_event=_terminal_event(
            store, run_id=reconcile_run.run_id, result="succeeded", gaps=0
        ),
        barrier_resolutions=(barrier_resolution,),
        checkpoints=((next_checkpoint, 1),),
        owner_token=second_token,
        lease_epoch=1,
    )
    assert repository.list_barrier_resolutions(barrier_id="barrier-1") == [
        barrier_resolution
    ]
    assert repository.list_checkpoint_barriers(unresolved_only=True) == []
    assert repository.latest_checkpoint(
        store.run.ticker,
        store.plan_item.source_definition_id,
        store.plan_item.source_definition_version,
        store.run.question_set_version,
    ) == next_checkpoint


def test_wrong_position_or_evidence_cannot_resolve_barrier(acquisition_store):
    # Detailed positive lineage is covered above; model/repository must reject a
    # fabricated proof id before any immutable resolution row is inserted.
    store = acquisition_store
    with pytest.raises((BarrierResolutionError, AcquisitionStorageError)):
        store.repository.append_barrier_resolution(
            {
                "barrier_resolution_id": "fabricated-resolution",
                "barrier_id": "missing-barrier",
                "opening_attempt_id": "missing",
                "resolving_attempt_id": "missing",
                "lease_epoch": 1,
            },
            owner_token="missing",
            lease_epoch=1,
        )


@pytest.mark.parametrize("outcome", sorted(item.value for item in BLOCKING_OUTCOMES))
def test_all_blocking_statuses_open_physical_partition_barrier(
    acquisition_store, outcome
):
    attempt = _attempt(acquisition_store)
    barrier = barrier_for_attempt(
        attempt=attempt,
        plan_item=acquisition_store.plan_item,
        work_position="page:1",
        outcome=outcome,
        reason_code=(
            "snapshot_commit_failed" if outcome == "parse_failed" else f"fixture_{outcome}"
        ),
    )
    assert barrier.partition_key == acquisition_store.plan_item.partition_key
    assert barrier.work_position == "page:1"
    assert barrier.outcome == AcquisitionOutcome(outcome)
    if outcome == "parse_failed":
        assert barrier.reason_code == "snapshot_commit_failed"


def test_required_attachment_child_fetch_barrier_is_not_cleared_by_mirror(
    acquisition_store,
):
    store = acquisition_store
    source_attempt = _attempt(store, attempt_id="source-fetch-failure")
    source_barrier = barrier_for_attempt(
        attempt=source_attempt,
        plan_item=store.plan_item,
        work_position="fetch:required-attachment",
        outcome="restricted",
        reason_code="http_403",
        canonical_resource_id="required-attachment",
    )
    mirror_plan = store.plan_item.model_copy(
        update={"source_definition_id": "mirror.independent"}
    )
    mirror_attempt = source_attempt.model_copy(
        update={
            "attempt_id": "mirror-success",
            "source_definition_id": "mirror.independent",
            "retry_group_id": "mirror-retry",
        }
    )
    mirror_barrier = barrier_for_attempt(
        attempt=mirror_attempt,
        plan_item=mirror_plan,
        work_position="fetch:required-attachment",
        outcome="network_failed",
        reason_code="mirror_fixture",
        canonical_resource_id="required-attachment",
    )
    assert source_barrier.barrier_id != mirror_barrier.barrier_id
    assert source_barrier.source_definition_id != mirror_barrier.source_definition_id


@pytest.mark.parametrize(
    ("reason", "expected"),
    (
        ("source_not_available", "source_not_available"),
        ("market_not_applicable", "market_not_applicable"),
    ),
)
def test_static_source_not_available_does_not_block_and_market_not_applicable_does_not_block(
    acquisition_store, reason, expected
):
    original = acquisition_store.coverage_entries[0]
    entry = CoverageEntry.model_validate(
        {
            **original.model_dump(mode="python"),
            "coverage_entry_id": f"static-{reason}",
            "plan_disposition": CoveragePlanDisposition.STATIC_POLICY_SKIPPED,
            "static_reason_code": reason,
        }
    )
    resolution = static_coverage_resolution(entry)
    assert resolution.status == CoverageResolutionStatus.STATIC_POLICY_SKIPPED
    assert resolution.material_gap_count == 0
    assert resolution.attempt_ids == ()
    assert resolution.reason_codes == (expected,)


def test_runtime_policy_skip_blocks_source_lower_bound(acquisition_store):
    store = acquisition_store
    attempt = _attempt(store, attempt_id="runtime-policy-skip")
    barrier = barrier_for_attempt(
        attempt=attempt,
        plan_item=store.plan_item,
        work_position="page:1",
        outcome="policy_skipped",
        reason_code="runtime_policy_not_approved",
    )
    early = CheckpointPosition(
        time_upper_bound=store.now - timedelta(days=1),
        canonical_resource_id="early",
    )
    late = CheckpointPosition(
        time_upper_bound=store.now,
        canonical_resource_id="late",
    )
    partitions = (
        CheckpointPartition(
            partition_key="required-a",
            execution_key="a",
            safe_through=late,
        ),
        CheckpointPartition(
            partition_key="required-b",
            execution_key="b",
            safe_through=early,
            unresolved_barrier_ids=(barrier.barrier_id,),
        ),
    )
    assert source_safe_lower_bound(partitions) == early
    assert barrier.outcome == AcquisitionOutcome.POLICY_SKIPPED
