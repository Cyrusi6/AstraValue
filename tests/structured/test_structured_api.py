from datetime import date

from fastapi.testclient import TestClient

from analysis.api import create_app
from analysis.models import SyncResult
from analysis.structured.exceptions import (
    StructuredBusyError,
    StructuredConflictError,
    StructuredIntegrityError,
)


class FakeStructuredService:
    def registry(self, *, limit, offset):
        rows = [{"dataset_id": f"D{index:04d}"} for index in range(620)]
        return {"total": len(rows), "items": rows[offset : offset + limit]}

    def fields(self, *, dataset_id, limit, offset):
        rows = [
            {"dataset_id": dataset_id or "D", "raw_name": f"field-{index:04d}"}
            for index in range(620)
        ]
        return {"total": len(rows), "items": rows[offset : offset + limit]}

    def resolve_company(self, query, *, market, as_of):
        return {
            "query": query,
            "market": market,
            "as_of": as_of or date(2026, 9, 8),
            "status": "resolved",
            "credential": "must-not-leak",
        }

    def industry_profile(self, ticker):
        return {"ticker": ticker, "status": "pending", "general_fallback_used": False}

    def peer_candidates(self, ticker, *, company_scope):
        return {"ticker": ticker, "company_scope": company_scope, "items": []}

    def plan(self, ticker, **kwargs):
        if kwargs["mode"] == "due" and ticker == "000000":
            raise ValueError("invalid due scope")
        return {"plan_id": "plan:1", "ticker": ticker, "performed_io": False}

    def run(self, run_id):
        if run_id == "conflict":
            raise StructuredConflictError("frozen version mismatch")
        if run_id == "busy":
            raise StructuredBusyError("lease busy")
        if run_id == "broken":
            raise StructuredIntegrityError("hash mismatch")
        return {"run_id": run_id, "status": "partial"}

    def resume(self, run_id):
        return self.run(run_id)

    def status(self, run_id):
        return {"run_id": run_id, "status": "planned"}

    def records(self, *, run_id, dataset_id, limit, offset):
        rows = [{"record_id": f"r{index:04d}"} for index in range(620)]
        return {"total": len(rows), "items": rows[offset : offset + limit]}

    def reading_tasks(self, *, run_id, limit, offset):
        return {"total": 0, "items": []}

    def coverage(self, snapshot_id, *, limit, offset):
        rows = [
            {
                "evaluation_id": f"e{index:04d}",
                "readiness": "reading_pending" if index == 619 else "ready",
            }
            for index in range(620)
        ]
        return {
            "coverage_snapshot_id": snapshot_id,
            "total": len(rows),
            "items": rows[offset : offset + limit],
        }

    def report(self, pack_dir, *, output_dir, formats, industry):
        return {
            "report_id": "report-1",
            "pack_dir": pack_dir,
            "output_dir": output_dir,
            "formats": formats,
            "industry": industry,
            "performed_network_io": False,
        }

    def sync(self, ticker, options):
        return SyncResult(
            ticker=ticker,
            provider_results={"structured": "queued"},
            source_strategy="structured-first-v1",
            structured_plan_id="plan:1",
            default_consume_eligible=False,
        )


def test_structured_resources_page_past_500_and_redact_credentials(service):
    client = TestClient(create_app(service, structured_service=FakeStructuredService()))
    registry = client.get("/api/structured/registry?limit=500&offset=120")
    assert registry.status_code == 200
    assert registry.json()["total"] == 620
    assert registry.json()["items"][-1]["dataset_id"] == "D0619"

    resolution = client.post(
        "/api/structured/company-resolution",
        json={"query": "600519", "market": "SSE"},
    )
    assert resolution.status_code == 200
    assert "credential" not in resolution.json()

    coverage = client.get(
        "/api/structured/research-coverage/snapshot:1?limit=500&offset=120"
    )
    assert coverage.json()["items"][-1]["readiness"] == "reading_pending"


def test_new_sync_returns_202_but_explicit_legacy_keeps_200(service):
    application = create_app(service, structured_service=FakeStructuredService())
    client = TestClient(application)
    queued = client.post(
        "/api/companies/600519/sync",
        json={"source_strategy": "structured-first-v1"},
    )
    assert queued.status_code == 202
    assert queued.json()["structured_plan_id"] == "plan:1"

    class LegacyManager:
        def sync(self, ticker, options):
            return SyncResult(
                ticker=ticker,
                provider_results={"legacy": "ok"},
                source_strategy="legacy-v1",
            )

    application.state.adapters = LegacyManager()
    legacy = client.post(
        "/api/companies/600519/sync",
        json={"source_strategy": "legacy-v1", "providers": ["baostock"]},
    )
    assert legacy.status_code == 200
    assert legacy.json()["source_strategy"] == "legacy-v1"


def test_structured_routes_expose_precise_error_statuses(service):
    client = TestClient(create_app(service, structured_service=FakeStructuredService()))
    invalid = client.post(
        "/api/structured/plans",
        json={"ticker": "000000", "mode": "due"},
    )
    assert invalid.status_code == 422
    assert client.post("/api/structured/runs/conflict/execute").status_code == 409
    assert client.post("/api/structured/runs/busy/execute").status_code == 503
    assert client.post("/api/structured/runs/broken/execute").status_code == 500


def test_unbound_structured_query_is_503(service):
    client = TestClient(create_app(service))
    response = client.get("/api/structured/registry")
    assert response.status_code == 503
    assert response.json()["error"] == "structured_service_unavailable"


def test_structured_report_api_contract_and_path_redaction(service):
    client = TestClient(create_app(service, structured_service=FakeStructuredService()))
    response = client.post(
        "/api/structured/reports",
        json={
            "pack_dir": "D:/private/lite-pack",
            "output_dir": "D:/private/reports",
            "formats": ["md", "html", "xlsx", "pdf"],
            "industry": "消费",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["report_id"] == "report-1"
    assert payload["formats"] == ["md", "html", "xlsx", "pdf"]
    assert payload["pack_dir"] != "D:/private/lite-pack"
    assert payload["output_dir"] != "D:/private/reports"
