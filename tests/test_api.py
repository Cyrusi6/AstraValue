from fastapi.testclient import TestClient

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


def test_document_ingest_and_fts_search(service, tmp_path):
    client = TestClient(create_app(service))
    source = tmp_path / "notice.txt"
    source.write_text("cashflow audit keyword and official notice", encoding="utf-8")
    response = client.post(
        "/api/documents",
        json={"ticker": "000001", "path": str(source), "title": "测试公告", "source_name": "本地正式文件"},
    )
    assert response.status_code == 201, response.text
    result = client.get("/api/documents/search", params={"q": "cashflow", "ticker": "000001"})
    assert result.status_code == 200
    assert result.json()[0]["title"] == "测试公告"

