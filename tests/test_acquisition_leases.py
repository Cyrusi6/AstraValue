from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from analysis.acquisition.models import AcquisitionAttempt, AcquisitionRun
from analysis.acquisition.repository import LeaseConflictError, StaleLeaseError


def _attempt(store, *, attempt_id: str, lease_epoch: int) -> AcquisitionAttempt:
    return AcquisitionAttempt(
        attempt_id=attempt_id,
        run_id=store.run.run_id,
        source_definition_id=store.plan_item.source_definition_id,
        source_definition_version=store.plan_item.source_definition_version,
        physical_query_plan_item_id=store.plan_item.plan_item_id,
        execution_key=store.plan_item.execution_key,
        attempt_kind="discovery",
        query_id=store.plan_item.query_id,
        work_position="page:1",
        retry_group_id=f"retry-{attempt_id}",
        lease_epoch=lease_epoch,
        started_at=store.now,
    )


def test_api_cli_race_has_one_lease_winner(acquisition_store):
    store = acquisition_store

    def claim(owner):
        try:
            lease, token = store.repository.claim_lease(
                store.run.run_id,
                owner_token=owner,
                now=store.now,
                ttl_seconds=30,
            )
            return ("won", lease.lease_epoch, token)
        except LeaseConflictError:
            return ("lost", None, owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("api-owner", "cli-owner")))
    assert sorted(item[0] for item in outcomes) == ["lost", "won"]
    assert next(item[1] for item in outcomes if item[0] == "won") == 1


def test_heartbeat_release_and_expired_reclaim_use_monotonic_epoch(acquisition_store):
    store = acquisition_store
    repository = store.repository
    first, first_token = repository.claim_lease(
        store.run.run_id,
        owner_token="first-owner",
        now=store.now,
        ttl_seconds=2,
    )
    renewed = repository.renew_lease(
        store.run.run_id,
        owner_token=first_token,
        lease_epoch=first.lease_epoch,
        now=store.now + timedelta(seconds=1),
        ttl_seconds=2,
    )
    assert renewed.expires_at == store.now + timedelta(seconds=3)
    with pytest.raises(LeaseConflictError):
        repository.claim_lease(
            store.run.run_id,
            owner_token="too-early",
            now=store.now + timedelta(seconds=2),
        )

    reclaimed, second_token = repository.claim_lease(
        store.run.run_id,
        owner_token="second-owner",
        now=store.now + timedelta(seconds=4),
    )
    assert reclaimed.lease_epoch == first.lease_epoch + 1
    with pytest.raises(StaleLeaseError):
        repository.save_attempt(
            _attempt(store, attempt_id="stale-attempt", lease_epoch=first.lease_epoch),
            owner_token=first_token,
        )
    repository.save_attempt(
        _attempt(store, attempt_id="current-attempt", lease_epoch=reclaimed.lease_epoch),
        owner_token=second_token,
    )
    released = repository.release_lease(
        store.run.run_id,
        owner_token=second_token,
        lease_epoch=reclaimed.lease_epoch,
        now=store.now + timedelta(seconds=5),
    )
    assert released.released_at == store.now + timedelta(seconds=5)
    with pytest.raises(StaleLeaseError):
        repository.renew_lease(
            store.run.run_id,
            owner_token=second_token,
            lease_epoch=reclaimed.lease_epoch,
            now=store.now + timedelta(seconds=6),
        )


@pytest.mark.parametrize(
    ("run_kind", "request_scope"),
    (("production", "complete"), ("smoke", "complete"), ("ad_hoc", "ad_hoc")),
)
def test_all_network_run_kinds_use_the_same_lease_contract(
    acquisition_store, run_kind, request_scope
):
    store = acquisition_store
    values = store.run.model_dump(mode="json")
    values.update(
        {
            "run_id": f"run-kind-{run_kind}",
            "run_kind": run_kind,
            "request_scope": request_scope,
        }
    )
    run = AcquisitionRun.model_validate(values)
    store.repository.save_run(run)
    lease, token = store.repository.claim_lease(
        run.run_id,
        owner_token=f"owner-{run_kind}",
        now=store.now,
    )
    assert lease.run_id == run.run_id
    assert lease.lease_epoch == 1
    assert token == f"owner-{run_kind}"
