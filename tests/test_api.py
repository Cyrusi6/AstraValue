import json

from fastapi.testclient import TestClient

from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.api import create_app


def test_api_report_method_lineage_and_export(service, demo_request):
    client = TestClient(create_app(service))
    assert client.get("/api/health").status_code == 200
    methods = client.get("/api/methods").json()
    assert any(item["method_id"] == "VAL.DCF.FCFF" for item in methods)
    method = client.get("/api/methods/VAL.DCF.FCFF/versions/1.0.0")
    assert method.status_code == 200
    assert "content_status: skeleton" in method.json()["documentation"]

    created = client.post("/api/reports", json=demo_request.model_dump(mode="json"))
    assert created.status_code == 201, created.text
    report = created.json()
    report_id = report["report_id"]
    assert len(report["sections"]) == 8
    assert client.get(f"/api/reports/{report_id}").status_code == 200
    assert client.get(f"/api/reports/{report_id}/changes").json()["changes"] == ["初始报告版本"]

    fact_id = report["facts"][0]["fact_id"]
    lineage = client.get(f"/api/facts/{fact_id}/lineage")
    assert lineage.status_code == 200
    assert len(lineage.json()["sources"]) == 2
    export = client.get(f"/api/reports/{report_id}/exports/md")
    assert export.status_code == 200
    assert "置顶结论卡" in export.content.decode("utf-8")


def test_document_ingest_without_bound_runtime_is_rejected(service, tmp_path):
    client = TestClient(create_app(service))
    source = tmp_path / "notice.txt"
    source.write_text("cashflow audit keyword and official notice", encoding="utf-8")
    response = client.post(
        "/api/documents",
        json={"ticker": "000001", "path": str(source), "title": "测试公告", "source_name": "本地正式文件"},
    )
    assert response.status_code == 503, response.text
    result = client.get("/api/documents/search", params={"q": "cashflow", "ticker": "000001"})
    assert result.status_code == 200
    assert result.json() == []


def _approved_manual_runtime(tmp_path):
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
    registry_path = tmp_path / "approved-registry.json"
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "evidence",
        registry_path=registry_path,
        orchestrator_factory=lambda _runtime: None,
    )


def test_approved_document_manual_ingest_root_is_the_bound_runtime(tmp_path):
    runtime = _approved_manual_runtime(tmp_path)
    client = TestClient(create_app(acquisition_runtime=runtime))
    source = tmp_path / "manual.txt"
    source.write_text("approved_manual_ingest_keyword evidence", encoding="utf-8")

    response = client.post(
        "/api/documents",
        json={
            "ticker": "600519",
            "path": str(source),
            "title": "已批准来源材料",
            "source_name": "不能决定正式来源的自由文本",
            "source_url": "https://static.cninfo.com.cn/manual/report.txt",
            "source_definition_id": "official",
        },
    )

    assert response.status_code == 201, response.text
    document = response.json()
    assert document["source"]["name"] == "巨潮资讯"
    assert document["archived_path"] == "[REDACTED_LOCAL_PATH]"
    assert document["text_path"] == "[REDACTED_LOCAL_PATH]"
    snapshot = runtime.repository.get_raw_resource_snapshot(
        document["raw_resource_snapshot_id"]
    )
    runtime.blob_store.resolve_blob(snapshot.archive_relative_path).relative_to(
        runtime.data_root
    )
    result = client.get(
        "/api/documents/search",
        params={"q": "approved_manual_ingest_keyword", "ticker": "600519"},
    )
    assert [item["document_id"] for item in result.json()] == [document["document_id"]]


def test_document_review_required_returns_candidate_and_no_formal_evidence(tmp_path):
    runtime = _approved_manual_runtime(tmp_path)
    client = TestClient(create_app(acquisition_runtime=runtime))
    source = tmp_path / "unknown.txt"
    source.write_text("must not enter evidence", encoding="utf-8")

    response = client.post(
        "/api/documents",
        json={
            "ticker": "600519",
            "path": str(source),
            "title": "未知来源",
            "source_name": "unknown",
            "source_url": "https://unknown.example.test/a?token=secret-value",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"] == "source_review_required"
    assert "secret-value" not in response.text
    assert runtime.repository.list_runs() == []
    assert len(runtime.repository.list_source_candidates()) == 1
    assert runtime.blob_store.scan_orphans(()) == ()
