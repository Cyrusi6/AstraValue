from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from analysis.acquisition.bootstrap import StorageNamespaceMismatch
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.api import app as default_app
from analysis.api import create_app
from analysis.cli import EXIT_OK, main
from analysis.service import AnalysisService
from analysis.storage import ReportStorage


NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


def _manual_ingest_registry(tmp_path: Path) -> Path:
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["registry_version"] = "1.1.0"
    definition = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "cninfo.disclosures"
    )
    definition["version"] = "1.1.0"
    definition["license_policy"]["save_derived_text"] = "allowed"
    definition["license_policy"]["llm_processing"] = "allowed"
    path = tmp_path / "manual-ingest-registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_one_composition_root_per_process_is_shared_by_api_consumers(tmp_path):
    assert default_app._application is None
    runtime = AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "evidence",
        orchestrator_factory=lambda _runtime: None,
    )

    app = create_app(acquisition_runtime=runtime)

    assert app.state.acquisition_runtime is runtime
    assert app.state.adapters is runtime.adapter_manager
    assert app.state.adapters.acquisition_runtime is runtime
    assert app.state.adapters.loaded_registry is runtime.loaded_registry
    assert app.state.service is runtime.analysis_service
    assert app.state.service.storage is runtime.report_storage
    assert default_app._application is None


def test_one_composition_root_binds_report_storage_clock_and_http_client(tmp_path):
    fixed_clock = lambda: NOW
    runtime = AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "evidence",
        clock=fixed_clock,
    )
    try:
        plan = runtime.plan_company_run(
            "600519",
            mode="baseline",
            listing_date=datetime(2001, 8, 27).date(),
        )
        definition = runtime.source_definition("cninfo.disclosures")
        runtime.orchestrator._adapter_for(definition)
        transport = runtime.orchestrator._transport_cache[
            (definition.source_definition_id, str(definition.version))
        ]

        assert plan.run.as_of == NOW
        assert runtime.orchestrator._now is fixed_clock
        assert transport._client is runtime.http_client
        assert transport._wall_clock is fixed_clock
        assert runtime.analysis_service.storage is runtime.report_storage
    finally:
        runtime.close()


def test_one_composition_root_per_process_for_bound_cli_serve(
    tmp_path,
    monkeypatch,
):
    served = []
    runtime_create_calls = []
    real_create = AcquisitionRuntime.create

    def create_runtime(*args, **kwargs):
        runtime_create_calls.append((args, kwargs))
        return real_create(*args, **kwargs)

    monkeypatch.setattr("analysis.cli.AcquisitionRuntime.create", create_runtime)
    monkeypatch.setattr(
        "uvicorn.run",
        lambda application, **options: served.append((application, options)),
    )

    code = main(
        [
            "serve",
            "--acquisition-db",
            str(tmp_path / "analysis.db"),
            "--acquisition-data-root",
            str(tmp_path / "evidence"),
        ]
    )

    assert code == EXIT_OK
    assert len(runtime_create_calls) == 1
    assert len(served) == 1
    explicit_app = served[0][0]
    assert explicit_app.state.acquisition_runtime is not None
    assert explicit_app.state.adapters.acquisition_runtime is explicit_app.state.acquisition_runtime
    assert (
        explicit_app.state.service.storage
        is explicit_app.state.acquisition_runtime.report_storage
    )
    assert default_app._application is None


def test_isolated_runtime_namespaces_share_only_the_workspace_source_gate(tmp_path):
    workspace = tmp_path / "workspace"
    first = AcquisitionRuntime.create(
        tmp_path / "first.db",
        tmp_path / "first-evidence",
        workspace_root=workspace,
        orchestrator_factory=lambda _runtime: None,
    )
    second = AcquisitionRuntime.create(
        tmp_path / "second.db",
        tmp_path / "second-evidence",
        workspace_root=workspace,
        orchestrator_factory=lambda _runtime: None,
    )
    plan = first.plan_company_run(
        "600519",
        mode="baseline",
        as_of=NOW,
        listing_date=datetime(2001, 8, 27).date(),
    )
    first_blob = first.blob_store.archive_bytes(b"namespace-one-only")

    assert first.namespace_id != second.namespace_id
    assert first.data_root != second.data_root
    assert second.repository.list_runs() == []
    assert first.repository.get_run(plan.run.run_id).run_id == plan.run.run_id
    assert first.blob_store.resolve_blob(first_blob.relative_path).is_file()
    assert not second.blob_store.resolve_blob(first_blob.relative_path).exists()
    assert first.source_gate.lock_directory == second.source_gate.lock_directory


def test_manual_ingest_root_is_injected_and_unknown_source_stays_candidate(tmp_path):
    registry_path = _manual_ingest_registry(tmp_path)
    runtime = AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "evidence",
        registry_path=registry_path,
        orchestrator_factory=lambda _runtime: None,
    )
    client = TestClient(create_app(acquisition_runtime=runtime))
    source = tmp_path / "registered.txt"
    source.write_text(
        "auditable_manual_ingest_token business model evidence",
        encoding="utf-8",
    )

    accepted = client.post(
        "/api/documents",
        json={
            "ticker": "600519",
            "path": str(source),
            "title": "已注册手工材料",
            "source_name": "自由文本不得决定正式来源身份",
            "source_url": "https://static.cninfo.com.cn/manual/registered.txt",
            "source_definition_id": "official",
        },
    )

    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["archived_path"] == "[REDACTED_LOCAL_PATH]"
    assert body["text_path"] == "[REDACTED_LOCAL_PATH]"
    assert body["source"]["name"] == "巨潮资讯"
    snapshot = runtime.repository.get_raw_resource_snapshot(
        body["raw_resource_snapshot_id"]
    )
    runtime.blob_store.resolve_blob(snapshot.archive_relative_path).relative_to(
        runtime.data_root
    )
    search = client.get(
        "/api/documents/search",
        params={"q": "auditable_manual_ingest_token", "ticker": "600519"},
    )
    assert search.status_code == 200
    assert [item["document_id"] for item in search.json()] == [body["document_id"]]

    unknown = tmp_path / "unknown.txt"
    unknown.write_text("must remain outside formal evidence", encoding="utf-8")
    before_runs = {item.run_id for item in runtime.repository.list_runs()}
    rejected = client.post(
        "/api/documents",
        json={
            "ticker": "600519",
            "path": str(unknown),
            "title": "待审核来源",
            "source_name": "unknown",
            "source_url": (
                "https://new-ir.example.test/report?access_token=must-not-leak"
            ),
        },
    )

    assert rejected.status_code == 409
    assert rejected.json()["error"] == "source_review_required"
    assert "must-not-leak" not in rejected.text
    assert {item.run_id for item in runtime.repository.list_runs()} == before_runs
    candidates = client.get("/api/source-candidates").json()
    assert len(candidates) == 1
    assert "must-not-leak" not in json.dumps(candidates)


def test_namespace_mismatch_is_rejected_before_a_second_runtime_is_built(tmp_path):
    database = tmp_path / "analysis.db"
    AcquisitionRuntime.create(
        database,
        tmp_path / "bound-evidence",
        orchestrator_factory=lambda _runtime: None,
    )

    with pytest.raises(StorageNamespaceMismatch):
        AcquisitionRuntime.create(
            database,
            tmp_path / "different-evidence",
            orchestrator_factory=lambda _runtime: None,
        )


def test_api_cli_same_store_can_read_the_same_durable_run(tmp_path, capsys):
    database = tmp_path / "analysis.db"
    data_root = tmp_path / "evidence"
    runtime = AcquisitionRuntime.create(
        database,
        data_root,
        orchestrator_factory=lambda _runtime: None,
    )
    client = TestClient(create_app(acquisition_runtime=runtime))
    created = client.post(
        "/api/companies/600519/acquisition-runs",
        json={
            "mode": "baseline",
            "as_of": NOW.isoformat(),
            "listing_date": "2001-08-27",
        },
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]

    code = main(
        [
            "acquire",
            "show",
            run_id,
            "--db",
            str(database),
            "--data-root",
            str(data_root),
            "--json",
        ]
    )

    assert code == EXIT_OK
    shown = json.loads(capsys.readouterr().out)
    assert shown["run"]["run_id"] == run_id
    assert shown["run"]["storage_namespace_id"] == runtime.namespace_id


def test_default_unchanged_without_explicit_acquisition_runtime(tmp_path):
    service = AnalysisService(storage=ReportStorage(tmp_path / "legacy.db"))
    app = create_app(service, acquisition_enabled=False)
    client = TestClient(app)

    assert app.state.acquisition_runtime is None
    assert app.state.adapters.acquisition_runtime is None
    assert client.get("/api/health").status_code == 200
    assert client.post(
        "/api/documents",
        json={
            "ticker": "600519",
            "path": str(tmp_path / "never-read.txt"),
            "title": "disabled",
            "source_name": "legacy",
        },
    ).status_code == 503
