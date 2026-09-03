from __future__ import annotations

from datetime import timedelta

from analysis.acquisition.adapters.base import FetchWork, QueryWork
from analysis.acquisition.models import (
    AcquisitionMode,
    AcquisitionOutcome,
    SnapshotIntegrityEvent,
    SnapshotIntegrityStatus,
)

from orchestrator_support import (
    NOW,
    ScenarioAdapter,
    discovery_result,
    envelope,
    make_runtime,
    resource,
    targeted_plan,
)


PDF_V1 = b"%PDF-1.4\nfixture-version-one\n%%EOF"
PDF_V2 = b"%PDF-1.4\nfixture-version-two\n%%EOF"


def _discovery(_response, work: QueryWork):
    return discovery_result(
        work,
        resources=(resource(),),
        declared_total=1,
    )


def _pdf_response(work: FetchWork, body: bytes, etag: str):
    return envelope(
        url=work.query.url,
        body=body,
        content_type="application/pdf",
        headers={"etag": etag, "last-modified": "Wed, 02 Sep 2026 08:00:00 GMT"},
    )


def _two_runs(runtime):
    first = targeted_plan(
        runtime,
        mode=AcquisitionMode.BASELINE,
        start_at=NOW - timedelta(days=1),
        as_of=NOW,
    )
    first_result = runtime.orchestrator.execute_run(first.run.run_id)
    second = targeted_plan(
        runtime,
        mode=AcquisitionMode.INCREMENTAL,
        start_at=NOW - timedelta(hours=12),
        as_of=NOW + timedelta(hours=12),
    )
    second_result = runtime.orchestrator.execute_run(second.run.run_id)
    return first, first_result, second, second_result


def test_304_requires_compatible_snapshot_anchor_and_reuses_snapshot(tmp_path) -> None:
    fetch_number = 0

    def fetch(work: FetchWork):
        nonlocal fetch_number
        fetch_number += 1
        if fetch_number == 1:
            return _pdf_response(work, PDF_V1, '"v1"')
        assert work.validators == {"etag": '"v1"', "last_modified": "Wed, 02 Sep 2026 08:00:00 GMT"}
        return envelope(
            url=work.query.url,
            body=b"",
            status=304,
            content_type="application/pdf",
            headers={"etag": '"v1"'},
        )

    adapter = ScenarioAdapter(
        lambda work: envelope(url=work.url),
        _discovery,
        fetch,
    )
    runtime = make_runtime(tmp_path / "valid-304", adapter)

    _, first_result, _, second_result = _two_runs(runtime)

    first_snapshot = runtime.repository.get_raw_resource_snapshot(
        runtime.repository.list_resource_observations(
            attempt_id=next(
                attempt_id
                for attempt_id in first_result.attempt_ids
                if runtime.repository.get_attempt(attempt_id).attempt_kind.value == "fetch"
            )
        )[0].snapshot_id
    )
    second_fetch = next(
        runtime.repository.get_attempt(attempt_id)
        for attempt_id in second_result.attempt_ids
        if runtime.repository.get_attempt(attempt_id).attempt_kind.value == "fetch"
    )
    observation = runtime.repository.list_resource_observations(
        attempt_id=second_fetch.attempt_id
    )[0]
    assert second_result.outcome_counts["unchanged"] == 1
    assert observation.snapshot_id == first_snapshot.snapshot_id
    assert observation.validator_source_snapshot_id == first_snapshot.snapshot_id


def test_304_without_snapshot_opens_barrier_then_unconditional_refetch_resolves_it(
    tmp_path,
) -> None:
    fetch_number = 0

    def fetch(work: FetchWork):
        nonlocal fetch_number
        fetch_number += 1
        if fetch_number == 1:
            assert work.validators == {}
            return envelope(
                url=work.query.url,
                body=b"",
                status=304,
                content_type="application/pdf",
            )
        assert work.validators == {}
        return _pdf_response(work, PDF_V1, '"fresh"')

    adapter = ScenarioAdapter(
        lambda work: envelope(url=work.url),
        _discovery,
        fetch,
    )
    runtime = make_runtime(tmp_path / "invalid-304", adapter)
    plan = targeted_plan(runtime)

    result = runtime.orchestrator.execute_run(plan.run.run_id)

    assert result.outcome_counts["parse_failed"] == 1
    assert result.outcome_counts["success"] == 2
    assert result.material_gap_count == 0
    assert len(runtime.repository.list_barrier_resolutions()) == 1
    assert runtime.repository.list_checkpoint_barriers(unresolved_only=True) == []
    fetch_attempts = [
        item
        for item in runtime.repository.list_attempts(run_id=plan.run.run_id)
        if item.attempt_kind.value == "fetch"
    ]
    assert fetch_attempts[1].supersedes_attempt_id == fetch_attempts[0].attempt_id
    first_terminal = next(
        event
        for event in runtime.repository.list_attempt_events(fetch_attempts[0].attempt_id)
        if event.outcome is not None
    )
    assert first_terminal.outcome == AcquisitionOutcome.PARSE_FAILED
    assert first_terminal.reason_code == "validator_anchor_missing"


def test_quarantined_anchor_makes_304_invalid_then_unconditional_refetch(tmp_path) -> None:
    phase = 0

    def fetch(work: FetchWork):
        nonlocal phase
        phase += 1
        if phase == 1:
            return _pdf_response(work, PDF_V1, '"v1"')
        if phase == 2:
            assert work.validators == {}
            return envelope(
                url=work.query.url,
                body=b"",
                status=304,
                content_type="application/pdf",
            )
        return _pdf_response(work, PDF_V2, '"v2"')

    adapter = ScenarioAdapter(
        lambda work: envelope(url=work.url),
        _discovery,
        fetch,
    )
    runtime = make_runtime(tmp_path / "quarantined-304", adapter)
    first = targeted_plan(runtime)
    first_result = runtime.orchestrator.execute_run(first.run.run_id)
    first_snapshot_id = next(
        observation.snapshot_id
        for attempt_id in first_result.attempt_ids
        for observation in runtime.repository.list_resource_observations(
            attempt_id=attempt_id
        )
        if observation.snapshot_id
    )
    runtime.repository.append_snapshot_integrity_event(
        SnapshotIntegrityEvent(
            snapshot_id=first_snapshot_id,
            status=SnapshotIntegrityStatus.QUARANTINED,
            reason_code="fixture_integrity_failure",
        )
    )
    second = targeted_plan(
        runtime,
        start_at=NOW - timedelta(hours=12),
        as_of=NOW + timedelta(hours=12),
    )

    result = runtime.orchestrator.execute_run(second.run.run_id)

    terminals = [
        event
        for attempt in runtime.repository.list_attempts(run_id=second.run.run_id)
        for event in runtime.repository.list_attempt_events(attempt.attempt_id)
        if event.outcome is not None
    ]
    assert any(event.reason_code == "invalid_304" for event in terminals)
    assert result.material_gap_count == 0
    latest = runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="fixture-resource-1",
    )
    assert latest.version == 2
    assert latest.supersedes_snapshot_id == first_snapshot_id


def test_hash_is_final_with_new_validator_old_etag_new_canonical_and_mixed_disposition(
    tmp_path,
) -> None:
    query_number = 0
    fetch_number = 0

    def query_response(work: QueryWork):
        nonlocal query_number
        query_number += 1
        return envelope(url=work.url)

    def parsed(_response, work: QueryWork):
        rows = (resource(),) if query_number == 1 else (
            resource(),
            resource("fixture-resource-2", url="https://static.cninfo.com.cn/finalpage/fixture-2.pdf"),
        )
        return discovery_result(work, resources=rows, declared_total=len(rows))

    def fetch(work: FetchWork):
        nonlocal fetch_number
        fetch_number += 1
        canonical = work.resource.canonical_resource_id
        if fetch_number == 1:
            return _pdf_response(work, PDF_V1, '"v1"')
        if canonical == "fixture-resource-1":
            # Changed validator, identical bytes: unchanged by SHA-256.
            return _pdf_response(work, PDF_V1, '"v2"')
        # New canonical identity is new evidence even if another resource has
        # used the same validator value.
        return _pdf_response(work, PDF_V2, '"v1"')

    adapter = ScenarioAdapter(query_response, parsed, fetch)
    runtime = make_runtime(tmp_path / "hash-final", adapter)

    _, _, _, result = _two_runs(runtime)

    assert result.outcome_counts["unchanged"] == 1
    assert result.outcome_counts["success"] == 2
    first = runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="fixture-resource-1",
    )
    second = runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.0.0",
        canonical_resource_id="fixture-resource-2",
    )
    assert first.version == 1
    assert second.version == 1
    observations = runtime.repository.list_resource_observations(
        snapshot_id=first.snapshot_id
    )
    assert {item.etag for item in observations} >= {'"v1"', '"v2"'}
