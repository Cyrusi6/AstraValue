from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from analysis.acquisition.models import AcquisitionRunEvent, AcquisitionRunEventType
from analysis.acquisition.repository import (
    AcquisitionStorageError,
    LeaseConflictError,
    StaleLeaseError,
    StorageBusyError,
)
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.acquisition.snapshots import SnapshotIntegrityMismatch
from analysis.api import create_app


NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


class FakeExecutor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def execute_run(self, run_id: str, *, lease_ttl_seconds: int = 60):
        self.calls.append((run_id, lease_ttl_seconds))
        if self.error is not None:
            raise self.error
        return {
            "run_id": run_id,
            "result": "succeeded",
            "coverage_accounted": True,
            "material_gap_count": 0,
            "default_consume_eligible": True,
            "checkpoint_ids": [],
        }


def _runtime(tmp_path, executor: FakeExecutor) -> AcquisitionRuntime:
    return AcquisitionRuntime.create(
        tmp_path / "acquisition.db",
        tmp_path / "evidence",
        orchestrator_factory=lambda _runtime: executor,
    )


def _create(client: TestClient) -> str:
    response = client.post(
        "/api/companies/600519/acquisition-runs",
        json={
            "mode": "baseline",
            "as_of": NOW.isoformat(),
            "listing_date": "2001-08-27",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def _client(tmp_path, executor: FakeExecutor):
    runtime = _runtime(tmp_path, executor)
    return runtime, TestClient(create_app(acquisition_runtime=runtime))


def test_create_is_plan_only_and_lists_attempts_observations_coverage(tmp_path):
    executor = FakeExecutor()
    runtime, client = _client(tmp_path, executor)

    run_id = _create(client)

    assert executor.calls == []
    assert runtime.repository.get_run(run_id).run_id == run_id
    detail = client.get(f"/api/acquisition-runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["summary"]["status"] == "planned"
    listed = client.get(
        "/api/acquisition-runs",
        params={"ticker": "600519", "mode": "baseline", "limit": 1},
    )
    assert listed.status_code == 200
    assert listed.json()[0]["run_id"] == run_id
    assert listed.json()[0]["status"] == "planned"
    coverage = client.get(f"/api/acquisition-runs/{run_id}/coverage")
    assert coverage.status_code == 200
    assert len(coverage.json()["entries"]) >= 10
    assert client.get(f"/api/acquisition-runs/{run_id}/attempts").json() == []
    assert client.get(f"/api/acquisition-runs/{run_id}/observations").json() == []
    definitions = client.get(
        "/api/source-definitions", params={"scope": "business_model"}
    ).json()
    assert len(definitions) == 4
    assert all("data_root" not in item for item in definitions)


def test_execute_passes_lease_ttl_and_returns_structured_result(tmp_path):
    executor = FakeExecutor()
    _runtime_value, client = _client(tmp_path, executor)
    run_id = _create(client)

    response = client.post(
        f"/api/acquisition-runs/{run_id}/execute",
        json={"lease_ttl_seconds": 15},
    )

    assert response.status_code == 200, response.text
    assert executor.calls == [(run_id, 15)]
    assert response.json()["coverage_accounted"] is True


def test_execute_lease_conflict_and_storage_busy_have_distinct_http_status(tmp_path):
    for error, expected, subtype in (
        (LeaseConflictError("held", NOW.isoformat()), 409, "active_lease"),
        (StorageBusyError("busy"), 503, "storage_busy"),
    ):
        _runtime_value, client = _client(tmp_path / subtype, FakeExecutor(error))
        run_id = _create(client)
        response = client.post(f"/api/acquisition-runs/{run_id}/execute", json={})
        assert response.status_code == expected
        assert response.json()["error"] == subtype
        assert "owner_token" not in response.text


def test_not_found_and_baseline_filter_rejected_are_validation_errors(tmp_path):
    _runtime_value, client = _client(tmp_path, FakeExecutor())
    for suffix in ("", "/attempts", "/coverage", "/observations"):
        assert client.get(f"/api/acquisition-runs/missing{suffix}").status_code == 404
    assert client.get("/api/raw-resource-snapshots/missing").status_code == 404
    assert client.get("/api/evidence-manifests/missing").status_code == 404

    filtered = client.post(
        "/api/companies/600519/acquisition-runs",
        json={
            "mode": "baseline",
            "listing_date": "2001-08-27",
            "start_at": "2026-01-01T00:00:00Z",
        },
    )
    assert filtered.status_code == 422

    ignored_ad_hoc_filter = client.post(
        "/api/companies/600519/acquisition-runs",
        json={
            "mode": "baseline",
            "run_kind": "ad_hoc",
            "listing_date": "2001-08-27",
            "question_ids": ["BM.Q01.IDENTITY_BUSINESS_MODEL"],
        },
    )
    assert ignored_ad_hoc_filter.status_code == 422


def test_list_pagination_filters_and_validation_are_stable(tmp_path):
    _runtime_value, client = _client(tmp_path, FakeExecutor())
    first = _create(client)
    second = _create(client)

    all_rows = client.get(
        "/api/acquisition-runs", params={"ticker": "600519", "mode": "baseline"}
    ).json()
    assert {item["run_id"] for item in all_rows} == {first, second}
    first_page = client.get(
        "/api/acquisition-runs", params={"ticker": "600519", "limit": 1}
    ).json()
    second_page = client.get(
        "/api/acquisition-runs",
        params={"ticker": "600519", "limit": 1, "offset": 1},
    ).json()
    assert first_page[0]["run_id"] != second_page[0]["run_id"]
    assert client.get("/api/acquisition-runs", params={"mode": "wrong"}).status_code == 422

    definitions = client.get(
        "/api/source-definitions",
        params={"scope": "business_model", "status": "enabled", "limit": 1},
    )
    assert definitions.status_code == 200
    assert len(definitions.json()) == 1
    assert client.get(
        "/api/source-definitions", params={"status": "unknown"}
    ).status_code == 422


def test_error_categories_and_metadata_are_redacted(tmp_path):
    cases = (
        (StaleLeaseError("stale owner"), 409, "stale_owner"),
        (AcquisitionStorageError("corrupt row at C:\\private\\analysis.db"), 500, "storage_integrity_error"),
        (SnapshotIntegrityMismatch("bad C:\\private\\blob.pdf"), 500, "integrity_error"),
        (AcquisitionStorageError("corrupt row at /tmp/private/analysis.db"), 500, "storage_integrity_error"),
    )
    for index, (error, status, subtype) in enumerate(cases):
        _runtime_value, client = _client(tmp_path / str(index), FakeExecutor(error))
        run_id = _create(client)
        response = client.post(f"/api/acquisition-runs/{run_id}/execute", json={})
        assert response.status_code == status
        assert response.json()["error"] == subtype
        assert "private" not in response.text

    secret_result = {
        "run_id": "run-secret",
        "result": "succeeded",
        "coverage_accounted": True,
        "material_gap_count": 0,
        "default_consume_eligible": True,
        "owner_token": "do-not-return",
        "database_path": "C:\\private\\analysis.db",
        "nested": {
            "api_key": "do-not-return-either",
            "local_path": "C:\\private\\blob.pdf",
            "source_url": "https://example.test/a?access_token=secret-value",
            "source_urls": [
                "https://example.test/b?api_key=list-secret-value"
            ],
            "local_paths": ["/tmp/list-private/blob.pdf"],
            "archive_relative_path": "raw/blobs/sha256/aa/aabbcc",
        },
        "db_path": "/tmp/private/analysis.db",
    }
    executor = FakeExecutor()
    executor_result = secret_result
    executor.execute_run = lambda run_id, lease_ttl_seconds=60: {
        **executor_result,
        "run_id": run_id,
    }
    _runtime_value, client = _client(tmp_path / "redaction", executor)
    run_id = _create(client)
    response = client.post(f"/api/acquisition-runs/{run_id}/execute", json={})
    assert response.status_code == 200
    body = response.text
    for secret in (
        "do-not-return",
        "do-not-return-either",
        "secret-value",
        "list-secret-value",
        "C:\\private",
        "/tmp/list-private",
        "/tmp/private",
    ):
        assert secret not in body
    assert response.json()["nested"]["archive_relative_path"] == (
        "raw/blobs/sha256/aa/aabbcc"
    )


def test_api_metadata_redacts_posix_absolute_paths_on_every_host(tmp_path):
    executor = FakeExecutor()
    executor.execute_run = lambda run_id, lease_ttl_seconds=60: {
        "run_id": run_id,
        "result": "succeeded",
        "coverage_accounted": True,
        "material_gap_count": 0,
        "default_consume_eligible": True,
        "archived_path": "/tmp/private/evidence/raw/blobs/example",
    }
    _runtime_value, client = _client(tmp_path, executor)
    run_id = _create(client)

    response = client.post(f"/api/acquisition-runs/{run_id}/execute", json={})

    assert response.status_code == 200
    assert response.json()["archived_path"] == "[REDACTED_LOCAL_PATH]"
    assert "/tmp/private" not in response.text


def test_checkpoint_integrity_events_and_manifest_metadata_are_redacted(
    tmp_path,
    monkeypatch,
):
    runtime, client = _client(tmp_path, FakeExecutor())
    assert client.get("/api/companies/600519/acquisition-checkpoints").json() == []

    monkeypatch.setattr(
        runtime.repository,
        "get_raw_resource_snapshot",
        lambda snapshot_id: SimpleNamespace(
            snapshot_id=snapshot_id,
            archive_absolute_path="C:\\private\\snapshot.pdf",
            canonical_url="https://example.test/report?token=secret-value",
        ),
    )
    monkeypatch.setattr(
        runtime.repository,
        "list_snapshot_integrity_events",
        lambda snapshot_id: [
            SimpleNamespace(
                snapshot_id=snapshot_id,
                status="verified",
                owner_token="secret-owner",
                checked_absolute_path="C:\\private\\snapshot.pdf",
            )
        ],
    )
    snapshot = client.get("/api/raw-resource-snapshots/snapshot-1")
    integrity = client.get(
        "/api/raw-resource-snapshots/snapshot-1/integrity-events"
    )
    assert snapshot.status_code == integrity.status_code == 200
    assert "secret-value" not in snapshot.text
    assert "C:\\private" not in snapshot.text
    assert "secret-owner" not in integrity.text
    assert "C:\\private" not in integrity.text

    manifest = SimpleNamespace(
        manifest_id="manifest-1",
        run_id="run-1",
        metadata={
            "owner_token": "manifest-owner-secret",
            "local_absolute_path": "C:\\private\\manifest.json",
        },
    )
    monkeypatch.setattr(
        runtime.repository,
        "get_evidence_manifest",
        lambda manifest_id: manifest,
    )
    monkeypatch.setattr(
        runtime.repository,
        "list_evidence_manifests",
        lambda **_kwargs: [manifest],
    )
    detail = client.get("/api/evidence-manifests/manifest-1")
    listed = client.get("/api/evidence-manifests")
    assert detail.status_code == listed.status_code == 200
    assert "manifest-owner-secret" not in detail.text + listed.text
    assert "C:\\private" not in detail.text + listed.text


class RepositoryLeaseExecutor:
    def __init__(self, runtime: AcquisitionRuntime) -> None:
        self.repository = runtime.repository
        self.io_count = 0

    def execute_run(self, run_id: str, *, lease_ttl_seconds: int = 60):
        lease, owner_token = self.repository.claim_lease(
            run_id,
            now=NOW,
            ttl_seconds=lease_ttl_seconds,
        )
        self.io_count += 1
        self.repository.release_lease(
            run_id,
            owner_token=owner_token,
            lease_epoch=lease.lease_epoch,
            now=NOW,
        )
        return {
            "run_id": run_id,
            "result": "succeeded",
            "coverage_accounted": True,
            "material_gap_count": 0,
            "default_consume_eligible": True,
            "lease_epoch": lease.lease_epoch,
        }


def test_expired_reclaim_resume_and_stale_owner_are_visible_through_api(tmp_path):
    runtime = AcquisitionRuntime.create(
        tmp_path / "acquisition.db",
        tmp_path / "evidence",
        orchestrator_factory=RepositoryLeaseExecutor,
    )
    client = TestClient(create_app(acquisition_runtime=runtime))
    run_id = _create(client)
    old_lease, old_owner = runtime.repository.claim_lease(
        run_id,
        now=NOW - timedelta(minutes=2),
        ttl_seconds=5,
    )

    response = client.post(
        f"/api/acquisition-runs/{run_id}/execute",
        json={"lease_ttl_seconds": 30},
    )
    assert response.status_code == 200
    assert response.json()["lease_epoch"] == old_lease.lease_epoch + 1
    assert runtime.orchestrator.io_count == 1

    with pytest.raises(StaleLeaseError):
        runtime.repository.append_run_event(
            AcquisitionRunEvent(
                run_id=run_id,
                event_type=AcquisitionRunEventType.RUNNING,
                occurred_at=NOW,
                lease_epoch=old_lease.lease_epoch,
            ),
            owner_token=old_owner,
        )


def test_business_model_sync_requires_injected_runtime(service):
    client = TestClient(create_app(service, acquisition_enabled=False))
    response = client.post(
        "/api/companies/600519/sync",
        json={"scopes": ["business_model"], "providers": ["official"]},
    )
    assert response.status_code == 503


def test_business_model_sync_rejects_mixed_legacy_scope_before_run_or_io(tmp_path):
    executor = FakeExecutor()
    runtime, client = _client(tmp_path, executor)

    response = client.post(
        "/api/companies/600519/sync",
        json={
            "scopes": ["business_model", "financials"],
            "providers": ["official"],
            "acquisition_mode": "baseline",
            "as_of": NOW.isoformat(),
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation"
    assert "不能与legacy同步范围混合执行" in response.json()["detail"]
    assert executor.calls == []
    assert runtime.repository.list_runs(ticker="600519") == []
