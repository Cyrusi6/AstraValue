from __future__ import annotations

from datetime import timedelta

import pytest

from analysis.acquisition.models import (
    AcquisitionAttempt,
    AcquisitionAttemptEvent,
    AcquisitionAttemptSegment,
    AcquisitionRun,
    CoverageEntry,
    PhysicalQueryCoverageLink,
    SourceCandidate,
)
from analysis.acquisition.repository import (
    AcquisitionStorageError,
    ImmutableRecordError,
)


def _attempt(store, *, attempt_id="attempt-1", retry_ordinal=0, supersedes=None):
    return AcquisitionAttempt(
        attempt_id=attempt_id,
        run_id=store.run.run_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        physical_query_plan_item_id=store.plan_item.plan_item_id,
        execution_key=store.plan_item.execution_key,
        attempt_kind="discovery",
        query_id=store.plan_item.query_id,
        time_start=store.plan_item.time_start,
        time_end=store.plan_item.time_end,
        page_number=1,
        work_position="page:1",
        retry_group_id="retry-storage-1",
        retry_ordinal=retry_ordinal,
        lease_epoch=1,
        started_at=store.now + timedelta(seconds=retry_ordinal),
        supersedes_attempt_id=supersedes,
    )


def test_registry_and_candidate_are_idempotent_immutable_and_paginated(acquisition_store):
    store = acquisition_store
    repository = store.repository
    repository.save_source_registry_version(store.registry)
    assert repository.get_source_registry_version(
        store.registry.registry_id, store.registry.registry_version
    ) == store.registry
    assert repository.list_source_registry_versions(limit=1) == [store.registry]
    repository.save_source_definition_version(store.definition)
    assert repository.get_source_definition_version(
        store.definition.source_definition_id, store.definition.version
    ) == store.definition
    assert store.definition in repository.list_source_definition_versions(
        definition_id=store.definition.source_definition_id
    )

    changed = store.registry.model_dump(mode="json")
    changed["effective_at"] = (store.now + timedelta(days=1)).isoformat()
    with pytest.raises(ImmutableRecordError):
        repository.save_source_registry_version(changed)
    changed_definition = store.definition.model_dump(mode="json")
    changed_definition["upstream_identity"] = "changed-upstream"
    with pytest.raises(ImmutableRecordError):
        repository.save_source_definition_version(changed_definition)

    candidate = SourceCandidate(
        candidate_id="candidate-1",
        candidate_url="https://candidate.example.test/investor-relations",
        candidate_domain="candidate.example.test",
        discovered_at=store.now,
        discovery_context={"ticker": "600519", "reason": "unregistered_link"},
    )
    repository.save_source_candidate(candidate)
    repository.save_source_candidate(candidate)
    assert repository.get_source_candidate(candidate.candidate_id) == candidate
    assert repository.list_source_candidates(status="pending_review", limit=1) == [
        candidate
    ]
    changed_candidate = candidate.model_dump(mode="json")
    changed_candidate["discovery_context"] = {"ticker": "000001"}
    with pytest.raises(ImmutableRecordError):
        repository.save_source_candidate(changed_candidate)


def test_run_physical_query_coverage_m2m_and_retry_attempts_are_stable(acquisition_store):
    store = acquisition_store
    repository = store.repository
    assert repository.get_run(store.run.run_id) == store.run
    assert repository.list_plan_items(store.run.run_id) == [store.plan_item]
    assert repository.list_coverage_entries(store.run.run_id) == list(
        store.coverage_entries
    )
    assert repository.list_plan_coverage_links(run_id=store.run.run_id) == list(
        store.links
    )

    lease, token = repository.claim_lease(
        store.run.run_id, owner_token="owner-storage", now=store.now
    )
    first = _attempt(store)
    second = _attempt(
        store,
        attempt_id="attempt-2",
        retry_ordinal=1,
        supersedes=first.attempt_id,
    )
    repository.save_attempt(first, owner_token=token)
    repository.save_attempt(second, owner_token=token)
    attempts = repository.list_attempts(plan_item_id=store.plan_item.plan_item_id)
    assert [item.attempt_id for item in attempts] == ["attempt-1", "attempt-2"]
    assert {item.physical_query_plan_item_id for item in attempts} == {
        store.plan_item.plan_item_id
    }
    assert lease.lease_epoch == 1


def test_plan_to_coverage_link_cannot_cross_run(acquisition_store):
    store = acquisition_store
    run_data = store.run.model_dump(mode="json")
    run_data["run_id"] = "run-storage-other"
    other_run = AcquisitionRun.model_validate(run_data)
    coverage_data = store.coverage_entries[0].model_dump(mode="json")
    coverage_data.update(
        {
            "coverage_entry_id": "coverage-storage-other",
            "run_id": other_run.run_id,
        }
    )
    other_coverage = CoverageEntry.model_validate(coverage_data)
    store.repository.save_run(other_run)
    store.repository.save_coverage_entry(other_coverage)
    with pytest.raises(AcquisitionStorageError, match="cross runs"):
        store.repository.save_plan_coverage_link(
            PhysicalQueryCoverageLink(
                plan_item_id=store.plan_item.plan_item_id,
                coverage_entry_id=other_coverage.coverage_entry_id,
            )
        )


def test_attempt_segments_terminal_and_abandoned_are_append_only_and_exclusive(
    acquisition_store,
):
    store = acquisition_store
    repository = store.repository
    _, token = repository.claim_lease(
        store.run.run_id, owner_token="owner-lifecycle", now=store.now
    )
    attempt = _attempt(store)
    repository.save_attempt(attempt, owner_token=token)
    segment = AcquisitionAttemptSegment(
        segment_id="segment-1",
        attempt_id=attempt.attempt_id,
        segment_ordinal=0,
        page_number=1,
        work_position="page:1",
        lease_epoch=1,
        committed_at=store.now + timedelta(seconds=1),
        next_safe_position="page:2",
    )
    repository.append_attempt_segment(segment, owner_token=token)
    repository.append_attempt_segment(segment, owner_token=token)
    assert repository.list_attempt_segments(attempt.attempt_id) == [segment]

    terminal = AcquisitionAttemptEvent(
        event_id="attempt-event-terminal",
        attempt_id=attempt.attempt_id,
        event_type="outcome_terminal",
        outcome="success",
        lease_epoch=1,
        occurred_at=store.now + timedelta(seconds=2),
    )
    repository.append_attempt_event(terminal, owner_token=token)
    repository.append_attempt_event(terminal, owner_token=token)
    with pytest.raises((ImmutableRecordError, AcquisitionStorageError)):
        repository.append_attempt_event(
            AcquisitionAttemptEvent(
                event_id="attempt-event-abandoned",
                attempt_id=attempt.attempt_id,
                event_type="abandoned",
                reason_code="process_exit",
                lease_epoch=1,
                occurred_at=store.now + timedelta(seconds=3),
            ),
            owner_token=token,
        )
    events = repository.list_attempt_events(attempt.attempt_id)
    assert events == [terminal]
    assert all(item.outcome != "timeout" for item in events)
