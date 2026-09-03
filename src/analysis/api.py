from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .adapters.manager import AdapterManager
from .documents import ingest_document
from .exports import export_report
from .models import (
    AssumptionPatchRequest,
    DocumentIngestRequest,
    FactVerificationRequest,
    ReportCreateRequest,
    ReviewRequest,
    SyncRequest,
    VerificationStatus,
)
from .registry import MethodRegistryError, PROJECT_ROOT
from .reporting import ReportBuildError
from .service import AnalysisService
from .storage import StorageError
from .verification import verify_pair


MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def create_app(service: AnalysisService | None = None) -> FastAPI:
    application = FastAPI(
        title="A股全行业八步财报分析系统",
        version="0.1.0",
        description="本地、可审计、版本冻结的个人投研API；不构成投资建议。",
    )
    application.state.service = service or AnalysisService()
    application.state.adapters = AdapterManager()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(StorageError)
    async def storage_error(_: Request, exc: StorageError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @application.exception_handler(MethodRegistryError)
    async def registry_error(_: Request, exc: MethodRegistryError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @application.exception_handler(ReportBuildError)
    async def report_error(_: Request, exc: ReportBuildError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @application.get("/api/health")
    def health(request: Request) -> dict:
        current = _service(request)
        return {
            "status": "ok",
            "methods": len(current.registry.list_methods(active_only=True)),
            "method_library_errors": len(current.registry.validate_library()),
        }

    @application.get("/api/methods")
    def list_methods(request: Request, include_inactive: bool = False) -> list[dict]:
        current = _service(request)
        return [
            {
                **item.model_dump(mode="json"),
                "method_ref": item.ref,
                "content_hash": current.registry._method_hash(item),
            }
            for item in current.registry.list_methods(active_only=not include_inactive)
        ]

    @application.get("/api/methods/{method_id}/versions/{version}")
    def get_method(method_id: str, version: str, request: Request) -> dict:
        current = _service(request)
        spec = current.registry.get(method_id, version)
        return {
            "spec": {**spec.model_dump(mode="json"), "method_ref": spec.ref},
            "documentation": current.registry.get_document(method_id, version),
            "content_hash": current.registry._method_hash(spec),
        }

    @application.post("/api/companies/{ticker}/sync")
    async def sync_company(
        ticker: str,
        request: Request,
        options: SyncRequest = Body(default_factory=SyncRequest),
    ) -> dict:
        current = _service(request)
        result = await run_in_threadpool(application.state.adapters.sync, ticker, options)
        current.storage.save_sync_result(result)
        current.timeseries.append_facts(result.facts)
        research_records = {
            "dimensional_facts": result.dimensional_facts,
            "events": result.events,
            "industry_facts": result.industry_facts,
            "forecast_snapshots": result.forecast_snapshots,
            "peer_sets": result.peer_sets,
        }
        current.timeseries.append_research_records(**research_records)
        research_snapshot_ids = sorted(
            {
                item.data_snapshot_id
                for records in research_records.values()
                for item in records
            }
        )
        for snapshot_id in research_snapshot_ids:
            current.timeseries.export_research_snapshot(
                snapshot_id,
                **research_records,
            )
        return result.model_dump(mode="json")

    @application.get("/api/timeseries/{ticker}")
    def get_timeseries(
        ticker: str,
        request: Request,
        metric_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> list[dict]:
        return _service(request).timeseries.query(ticker, metric_id, limit)

    @application.get("/api/reports")
    def list_reports(
        request: Request,
        ticker: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict]:
        return [item.model_dump(mode="json") for item in _service(request).storage.list_reports(ticker, limit)]

    @application.post("/api/reports", status_code=201)
    def create_report(payload: ReportCreateRequest, request: Request) -> dict:
        return _service(request).create_report(payload).model_dump(mode="json")

    @application.get("/api/reports/{report_id}")
    def get_report(report_id: str, request: Request) -> dict:
        return _service(request).storage.get_report(report_id).model_dump(mode="json")

    @application.patch("/api/reports/{report_id}/assumptions")
    def patch_assumptions(report_id: str, payload: AssumptionPatchRequest, request: Request) -> dict:
        return _service(request).patch_assumptions(report_id, payload).model_dump(mode="json")

    @application.post("/api/reports/{report_id}/recalculate")
    def recalculate(report_id: str, request: Request) -> dict:
        return _service(request).recalculate(report_id).model_dump(mode="json")

    @application.post("/api/reports/{report_id}/reanalyze")
    def reanalyze(report_id: str, request: Request) -> dict:
        return _service(request).reanalyze(report_id).model_dump(mode="json")

    @application.post("/api/reports/{report_id}/review")
    def review(report_id: str, payload: ReviewRequest, request: Request) -> dict:
        try:
            report = _service(request).review(report_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return report.model_dump(mode="json")

    @application.get("/api/reports/{report_id}/changes")
    def report_changes(report_id: str, request: Request) -> dict:
        return _service(request).changes(report_id)

    @application.get("/api/reports/{report_id}/exports/{format}")
    async def report_export(report_id: str, format: str, request: Request):
        normalized = format.lower()
        if normalized not in MEDIA_TYPES:
            raise HTTPException(status_code=404, detail="仅支持md、html、xlsx和pdf")
        report = _service(request).storage.get_report(report_id)
        path = await run_in_threadpool(export_report, report, normalized)
        return FileResponse(path, media_type=MEDIA_TYPES[normalized], filename=path.name)

    @application.get("/api/facts/{fact_id}/lineage")
    def fact_lineage(fact_id: str, request: Request) -> dict:
        return _service(request).storage.fact_lineage(fact_id)

    @application.get("/api/companies/{ticker}/dimensions")
    def list_dimensional_facts(
        ticker: str,
        request: Request,
        metric_id: str | None = None,
        dimension_type: str | None = None,
        as_of: datetime | None = None,
        data_snapshot_id: str | None = None,
        verification_status: VerificationStatus | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> list[dict]:
        records = _service(request).storage.list_dimensional_facts(
            ticker,
            metric_id=metric_id,
            dimension_type=dimension_type,
            as_of=as_of,
            data_snapshot_id=data_snapshot_id,
            verification_status=verification_status,
            limit=limit,
        )
        return [item.model_dump(mode="json") for item in records]

    @application.get("/api/companies/{ticker}/dimensions/review-queue")
    def dimensional_review_queue(
        ticker: str,
        request: Request,
        data_snapshot_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict:
        records = _service(request).storage.list_dimensional_facts(
            ticker,
            data_snapshot_id=data_snapshot_id,
            verification_status=VerificationStatus.PENDING,
            limit=limit,
        )
        return {
            "ticker": ticker,
            "verification_status": VerificationStatus.PENDING.value,
            "count": len(records),
            "records": [item.model_dump(mode="json") for item in records],
        }

    @application.get("/api/dimensional-facts/{dimensional_fact_id}/lineage")
    def dimensional_fact_lineage(dimensional_fact_id: str, request: Request) -> dict:
        return _service(request).storage.dimensional_fact_lineage(dimensional_fact_id)

    @application.get("/api/companies/{ticker}/events")
    def list_events(
        ticker: str,
        request: Request,
        event_type: str | None = None,
        root_event_id: str | None = None,
        as_of: datetime | None = None,
        data_snapshot_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> list[dict]:
        records = _service(request).storage.list_events(
            ticker,
            event_type=event_type,
            root_event_id=root_event_id,
            as_of=as_of,
            data_snapshot_id=data_snapshot_id,
            limit=limit,
        )
        return [item.model_dump(mode="json") for item in records]

    @application.get("/api/events/{event_id}/lineage")
    def event_lineage(event_id: str, request: Request) -> dict:
        return _service(request).storage.event_lineage(event_id)

    @application.get("/api/companies/{ticker}/announcements")
    def list_announcements(
        ticker: str,
        request: Request,
        event_type: str | None = None,
        as_of: datetime | None = None,
        data_snapshot_id: str | None = None,
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> list[dict]:
        records = _service(request).storage.list_announcements(
            ticker,
            event_type=event_type,
            as_of=as_of,
            data_snapshot_id=data_snapshot_id,
            limit=limit,
        )
        return [item.model_dump(mode="json") for item in records]

    @application.get("/api/announcements/{announcement_record_id}/lineage")
    def announcement_lineage(announcement_record_id: str, request: Request) -> dict:
        return _service(request).storage.announcement_lineage(announcement_record_id)

    @application.get("/api/industries/{industry_code}/facts")
    def list_industry_facts(
        industry_code: str,
        request: Request,
        metric_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> list[dict]:
        records = _service(request).storage.list_industry_facts(
            industry_code,
            metric_id=metric_id,
            as_of=as_of,
            limit=limit,
        )
        return [item.model_dump(mode="json") for item in records]

    @application.get("/api/industry-facts/{industry_fact_id}/lineage")
    def industry_fact_lineage(industry_fact_id: str, request: Request) -> dict:
        return _service(request).storage.industry_fact_lineage(industry_fact_id)

    @application.get("/api/forecasts/{ticker}/snapshots")
    def list_forecast_snapshots(
        ticker: str,
        request: Request,
        metric_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> list[dict]:
        records = _service(request).storage.list_forecast_snapshots(
            ticker,
            metric_id=metric_id,
            as_of=as_of,
            limit=limit,
        )
        return [item.model_dump(mode="json") for item in records]

    @application.get("/api/forecasts/{forecast_snapshot_id}/lineage")
    def forecast_snapshot_lineage(forecast_snapshot_id: str, request: Request) -> dict:
        return _service(request).storage.forecast_snapshot_lineage(forecast_snapshot_id)

    @application.get("/api/peer-sets/{peer_set_id}/versions/{version}")
    def get_peer_set_version(
        peer_set_id: str,
        version: int,
        request: Request,
    ) -> dict:
        peer_set = _service(request).storage.get_peer_set_version(peer_set_id, version)
        return peer_set.model_dump(mode="json")

    @application.get("/api/peer-sets/{peer_set_id}/versions/{version}/lineage")
    def peer_set_lineage(
        peer_set_id: str,
        version: int,
        request: Request,
    ) -> dict:
        return _service(request).storage.peer_set_lineage(peer_set_id, version)

    @application.post("/api/facts/verify")
    def verify_facts(payload: FactVerificationRequest, request: Request) -> dict:
        storage = _service(request).storage
        left = storage.get_fact(payload.left_fact_id)
        right = storage.get_fact(payload.right_fact_id)
        source_ids = set(left.source_ids + right.source_ids)
        sources = {source_id: storage.get_source(source_id) for source_id in source_ids}
        outcome = verify_pair(left, right, sources, payload.relative_tolerance)
        return {
            "status": outcome.status.value,
            "accepted_value": outcome.accepted_value,
            "accepted_fact_id": outcome.accepted_fact_id,
            "relative_difference": outcome.relative_difference,
            "conflict_dimensions": outcome.conflict_dimensions,
            "reasons": outcome.reasons,
        }

    @application.post("/api/documents", status_code=201)
    def add_document(payload: DocumentIngestRequest, request: Request) -> dict:
        document = ingest_document(payload)
        storage = _service(request).storage
        storage.save_sources([document.source])
        storage.save_document(document)
        return document.model_dump(mode="json")

    @application.get("/api/documents/search")
    def search_documents(
        request: Request,
        q: str = Query(min_length=1),
        ticker: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[dict]:
        return _service(request).storage.search_documents(q, ticker, limit)

    frontend_dist = PROJECT_ROOT / "frontend" / "dist"
    assets = frontend_dist / "assets"
    if assets.exists():
        from fastapi.staticfiles import StaticFiles

        application.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API路径不存在")
        index = frontend_dist / "index.html"
        if index.exists():
            return FileResponse(index)
        return {
            "message": "前端尚未构建，请运行 cd frontend; npm install; npm run build",
            "api_docs": "/docs",
        }

    return application


def _service(request: Request) -> AnalysisService:
    return request.app.state.service


app = create_app()
