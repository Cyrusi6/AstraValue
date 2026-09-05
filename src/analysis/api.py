from __future__ import annotations

import threading
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .adapters.manager import AdapterManager
from .documents import SourceReviewRequired, ingest_registered_document
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
from .acquisition.bootstrap import (
    BootstrapLockTimeout,
    StorageNamespaceMismatch,
    StorageRepairRequired,
)
from .acquisition.models import (
    AcquisitionMode,
    AcquisitionRunEventType,
    AcquisitionRunKind,
    SourceCandidateStatus,
    SourcePolicyStatus,
)
from .acquisition.planner import AcquisitionPlanningError
from .acquisition.registry import SourceRegistryError, SourceRegistryLoader
from .acquisition.repository import (
    AcquisitionNotFoundError,
    AcquisitionStorageError,
    LeaseConflictError,
    StaleLeaseError,
    StorageBusyError,
)
from .acquisition.runtime import AcquisitionRuntime, acquisition_v1_enabled
from .acquisition.security import (
    REDACTED_LOCAL_PATH,
    is_sensitive_name,
    looks_like_absolute_local_path,
    redact_absolute_local_paths,
    redact_url,
)
from .acquisition.snapshots import SnapshotPipelineError


MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


class AcquisitionRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AcquisitionMode
    as_of: datetime | None = None
    company_name: str | None = None
    market: str | None = Field(default=None, pattern="^(SSE|SZSE)$")
    listing_date: date | None = None
    prospectus_date: date | None = None
    run_kind: AcquisitionRunKind = AcquisitionRunKind.PRODUCTION
    parent_run_id: str | None = None
    # These fields are accepted only to return a precise contract error rather
    # than silently narrowing a production baseline.
    start_at: datetime | None = None
    question_ids: list[str] | None = None


class AcquisitionExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_ttl_seconds: int = Field(default=60, ge=5, le=3600)


def create_app(
    service: AnalysisService | None = None,
    acquisition_runtime: AcquisitionRuntime | None = None,
    *,
    acquisition_enabled: bool | None = None,
) -> FastAPI:
    application = FastAPI(
        title="A股全行业八步财报分析系统",
        version="0.1.0",
        description="本地、可审计、版本冻结的个人投研API；不构成投资建议。",
    )
    enabled = acquisition_v1_enabled(acquisition_enabled)
    if acquisition_runtime is not None and acquisition_enabled is None:
        # Explicit dependency injection is an explicit local enablement.  The
        # environment flag only controls implicit application startup.
        enabled = True
    bound_runtime = acquisition_runtime if enabled else None
    if bound_runtime is not None:
        service = (
            bound_runtime.analysis_service
            if service is None
            else bound_runtime.bind_analysis_service(service)
        )
    application.state.service = service or AnalysisService()
    application.state.acquisition_runtime = bound_runtime
    application.state.acquisition_v1_enabled = enabled
    application.state.adapters = (
        bound_runtime.adapter_manager
        if bound_runtime is not None
        else AdapterManager()
    )
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

        return JSONResponse(
            status_code=404,
            content={"detail": _safe_error_detail(exc)},
        )

    @application.exception_handler(AcquisitionNotFoundError)
    async def acquisition_not_found(_: Request, exc: AcquisitionNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"detail": _safe_error_detail(exc), "error": "not_found"},
        )

    @application.exception_handler(LeaseConflictError)
    async def acquisition_active_lease(_: Request, exc: LeaseConflictError):
        return JSONResponse(
            status_code=409,
            content={
                "detail": "采集运行已有未过期执行租约",
                "error": "active_lease",
                "run_id": exc.run_id,
                "expires_at": exc.expires_at,
                "retryable": True,
            },
        )

    @application.exception_handler(StaleLeaseError)
    async def acquisition_stale_owner(_: Request, exc: StaleLeaseError):
        return JSONResponse(
            status_code=409,
            content={
                "detail": _safe_error_detail(exc),
                "error": "stale_owner",
                "retryable": False,
            },
        )

    @application.exception_handler(SourceReviewRequired)
    async def source_review_required(_: Request, exc: SourceReviewRequired):
        return JSONResponse(
            status_code=409,
            content={
                "detail": _safe_error_detail(exc),
                "error": "source_review_required",
                "candidate_id": exc.candidate_id,
            },
        )

    @application.exception_handler(StorageBusyError)
    async def acquisition_storage_busy(_: Request, exc: StorageBusyError):
        return JSONResponse(
            status_code=503,
            content={
                "detail": _safe_error_detail(exc),
                "error": "storage_busy",
                "retryable": True,
            },
        )

    @application.exception_handler(SourceRegistryError)
    @application.exception_handler(AcquisitionPlanningError)
    @application.exception_handler(StorageNamespaceMismatch)
    @application.exception_handler(ValueError)
    async def acquisition_validation(_: Request, exc: Exception):
        return JSONResponse(
            status_code=422,
            content={"detail": _safe_error_detail(exc), "error": "validation"},
        )

    @application.exception_handler(BootstrapLockTimeout)
    async def acquisition_bootstrap_busy(_: Request, exc: Exception):
        return JSONResponse(
            status_code=503,
            content={
                "detail": _safe_error_detail(exc),
                "error": "storage_busy",
                "retryable": True,
            },
        )

    @application.exception_handler(StorageRepairRequired)
    async def acquisition_storage_integrity(_: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "detail": _safe_error_detail(exc),
                "error": "storage_integrity_error",
                "retryable": False,
            },
        )

    @application.exception_handler(SnapshotPipelineError)
    async def acquisition_integrity(_: Request, exc: SnapshotPipelineError):
        return JSONResponse(
            status_code=500,
            content={"detail": _safe_error_detail(exc), "error": "integrity_error"},
        )

    @application.exception_handler(AcquisitionStorageError)
    async def acquisition_storage_error(_: Request, exc: AcquisitionStorageError):
        return JSONResponse(
            status_code=500,
            content={
                "detail": _safe_error_detail(exc),
                "error": "storage_integrity_error",
            },
        )

    @application.exception_handler(MethodRegistryError)
    async def registry_error(_: Request, exc: MethodRegistryError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=404,
            content={"detail": _safe_error_detail(exc)},
        )

    @application.exception_handler(ReportBuildError)
    async def report_error(_: Request, exc: ReportBuildError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=422,
            content={"detail": _safe_error_detail(exc)},
        )

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

    @application.get("/api/source-definitions")
    def list_source_definitions(
        request: Request,
        scope: str | None = None,
        status: SourcePolicyStatus | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        runtime = _optional_acquisition_runtime(request)
        definitions = (
            runtime.loaded_registry.registry.definitions
            if runtime is not None
            else SourceRegistryLoader().load_registry().registry.definitions
        )
        rows = [
            item
            for item in definitions
            if (scope is None or scope in item.scopes)
            and (status is None or item.policy_status == status)
        ]
        rows = sorted(rows, key=lambda item: (item.source_definition_id, item.version))
        return [
            {
                "source_definition_id": item.source_definition_id,
                "version": item.version,
                "display_name": item.display_name,
                "upstream_identity": item.upstream_identity,
                "authority_level": item.authority_level,
                "policy_status": item.policy_status.value,
                "enabled": item.enabled,
                "scopes": list(item.scopes),
                "scope_version": item.scope_version,
                "topics": list(item.topics),
                "refresh_frequency": item.refresh_frequency,
                "llm_processing": item.license_policy.llm_processing.value,
                "archive_original": item.license_policy.archive_original.value,
                "query_count": len(item.queries),
            }
            for item in rows[offset : offset + limit]
        ]

    @application.get("/api/source-candidates")
    def list_source_candidates(
        request: Request,
        status: SourceCandidateStatus | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        rows = _acquisition_runtime(request).repository.list_source_candidates(
            status=None if status is None else status.value,
            limit=limit,
            offset=offset,
        )
        return [_redacted(item) for item in rows]

    @application.post("/api/companies/{ticker}/acquisition-runs", status_code=201)
    def create_acquisition_run(
        ticker: str,
        payload: AcquisitionRunCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        if payload.start_at is not None or payload.question_ids is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "baseline/incremental/reconcile执行范围只能由锚点、checkpoint与"
                    "registry决定；本端点不接受被静默忽略的时间或问题过滤"
                ),
            )
        runtime = _acquisition_runtime(request)
        plan = runtime.plan_company_run(
            ticker,
            mode=payload.mode,
            as_of=payload.as_of,
            company_name=payload.company_name,
            market=payload.market,
            listing_date=payload.listing_date,
            prospectus_date=payload.prospectus_date,
            run_kind=payload.run_kind,
            parent_run_id=payload.parent_run_id,
            persist=True,
        )
        return _plan_response(plan)

    @application.post("/api/acquisition-runs/{run_id}/execute")
    async def execute_acquisition_run(
        run_id: str,
        request: Request,
        payload: AcquisitionExecuteRequest = Body(default_factory=AcquisitionExecuteRequest),
    ) -> dict[str, Any]:
        runtime = _acquisition_runtime(request)
        if runtime.orchestrator is None:
            raise HTTPException(status_code=503, detail="采集执行器尚不可用")
        executor = getattr(runtime.orchestrator, "execute_run", None) or getattr(
            runtime.orchestrator, "execute", None
        )
        if executor is None:
            raise HTTPException(status_code=503, detail="采集执行器接口不可用")
        result = await run_in_threadpool(
            executor,
            run_id,
            lease_ttl_seconds=payload.lease_ttl_seconds,
        )
        return _redacted(result)

    @application.get("/api/acquisition-runs")
    def list_acquisition_runs(
        request: Request,
        ticker: str | None = None,
        mode: AcquisitionMode | None = None,
        run_kind: AcquisitionRunKind | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        rows = _acquisition_runtime(request).repository.list_runs(
            ticker=ticker,
            mode=None if mode is None else mode.value,
            run_kind=None if run_kind is None else run_kind.value,
            limit=limit,
            offset=offset,
        )
        repository = _acquisition_runtime(request).repository
        return [_run_summary(repository, item) for item in rows]

    @application.get("/api/acquisition-runs/{run_id}")
    def get_acquisition_run(run_id: str, request: Request) -> dict[str, Any]:
        repository = _acquisition_runtime(request).repository
        run = repository.get_run(run_id)
        return {
            "run": _redacted(run),
            "events": [_redacted(item) for item in repository.list_run_events(run_id)],
            "summary": _run_summary(repository, run),
            "physical_query_plan_items": len(repository.list_plan_items(run_id)),
            "coverage_entries": len(repository.list_coverage_entries(run_id)),
        }

    @application.get("/api/acquisition-runs/{run_id}/attempts")
    def list_acquisition_attempts(
        run_id: str,
        request: Request,
        source_definition_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        repository = _acquisition_runtime(request).repository
        repository.get_run(run_id)
        attempts = repository.list_attempts(
                run_id=run_id,
                source_definition_id=source_definition_id,
                limit=limit,
                offset=offset,
        )
        return [_attempt_summary(repository, item) for item in attempts]

    @application.get("/api/acquisition-runs/{run_id}/coverage")
    def list_acquisition_coverage(
        run_id: str,
        request: Request,
        source_definition_id: str | None = None,
        question_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        repository = _acquisition_runtime(request).repository
        repository.get_run(run_id)
        entries = [
            item
            for item in repository.list_coverage_entries(run_id)
            if (
                source_definition_id is None
                or item.source_definition_id == source_definition_id
            )
            and (question_id is None or item.question_id == question_id)
        ]
        page = entries[offset : offset + limit]
        entry_ids = {item.coverage_entry_id for item in page}
        return {
            "entries": [_redacted(item) for item in page],
            "links": [
                _redacted(item)
                for item in repository.list_plan_coverage_links(run_id=run_id)
                if item.coverage_entry_id in entry_ids
            ],
            "resolutions": [
                _redacted(item)
                for item in repository.list_coverage_resolutions(run_id)
                if item.coverage_entry_id in entry_ids
            ],
            "total": len(entries),
            "limit": limit,
            "offset": offset,
        }

    @application.get("/api/acquisition-runs/{run_id}/observations")
    def list_acquisition_observations(
        run_id: str,
        request: Request,
        observation_kind: str | None = Query(
            default=None, pattern="^(discovery|resource)$"
        ),
        source_definition_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        repository = _acquisition_runtime(request).repository
        repository.get_run(run_id)
        attempts = repository.list_attempts(
            run_id=run_id,
            source_definition_id=source_definition_id,
            limit=None,
        )
        rows: list[dict[str, Any]] = []
        for attempt in attempts:
            if observation_kind in (None, "discovery"):
                rows.extend(
                    {
                        "observation_kind": "discovery",
                        **_redacted(item),
                    }
                    for item in repository.list_discovery_observations(
                        attempt.attempt_id
                    )
                )
            if observation_kind in (None, "resource"):
                rows.extend(
                    {
                        "observation_kind": "resource",
                        **_redacted(item),
                    }
                    for item in repository.list_resource_observations(
                        attempt_id=attempt.attempt_id,
                        limit=None,
                    )
                )
        rows.sort(
            key=lambda item: (
                str(item.get("observed_at") or ""),
                str(item.get("observation_id") or ""),
                str(item.get("observation_kind") or ""),
            )
        )
        return rows[offset : offset + limit]

    @application.get("/api/companies/{ticker}/acquisition-checkpoints")
    def list_acquisition_checkpoints(
        ticker: str,
        request: Request,
        source_definition_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        runtime = _acquisition_runtime(request)
        market = runtime.build_profile(ticker).market
        rows = []
        for definition in runtime.loaded_registry.registry.definitions:
            if (
                "business_model" not in definition.scopes
                or not definition.applies_to(ticker, market)
                or (
                    source_definition_id is not None
                    and definition.source_definition_id != source_definition_id
                )
            ):
                continue
            checkpoint = runtime.repository.latest_checkpoint(
                ticker,
                definition.source_definition_id,
                definition.version,
                runtime.loaded_questions.question_set.version,
            )
            if checkpoint is not None:
                rows.append(_redacted(checkpoint))
        rows.sort(
            key=lambda item: (
                str(item.get("source_definition_id") or ""),
                int(item.get("checkpoint_version") or 0),
            )
        )
        return rows[offset : offset + limit]

    @application.get("/api/raw-resource-snapshots/{snapshot_id}")
    def get_raw_resource_snapshot(snapshot_id: str, request: Request) -> dict[str, Any]:
        return _redacted(
            _acquisition_runtime(request).repository.get_raw_resource_snapshot(snapshot_id)
        )

    @application.get("/api/raw-resource-snapshots/{snapshot_id}/integrity-events")
    def list_snapshot_integrity_events(
        snapshot_id: str,
        request: Request,
    ) -> list[dict[str, Any]]:
        repository = _acquisition_runtime(request).repository
        repository.get_raw_resource_snapshot(snapshot_id)
        return [
            _redacted(item)
            for item in repository.list_snapshot_integrity_events(snapshot_id)
        ]

    @application.get("/api/evidence-manifests/{manifest_id}")
    def get_evidence_manifest(manifest_id: str, request: Request) -> dict[str, Any]:
        return _redacted(
            _acquisition_runtime(request).repository.get_evidence_manifest(manifest_id)
        )

    @application.get("/api/evidence-manifests")
    def list_evidence_manifests(
        request: Request,
        run_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        rows = _acquisition_runtime(request).repository.list_evidence_manifests(
            run_id=run_id,
            limit=limit,
            offset=offset,
        )
        return [_redacted(item) for item in rows]

    @application.post("/api/companies/{ticker}/sync")
    async def sync_company(
        ticker: str,
        request: Request,
        options: SyncRequest = Body(default_factory=SyncRequest),
    ) -> dict:
        current = _service(request)
        if (
            "business_model" in options.scopes
            and _optional_acquisition_runtime(request) is None
        ):
            raise HTTPException(
                status_code=503,
                detail=(
                    "business_model采集必须启用并注入同一database/data_root的"
                    "AcquisitionRuntime"
                ),
            )
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
        return _redacted(result)

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
        runtime = _acquisition_runtime(request)
        document = ingest_registered_document(payload, runtime)
        storage = _service(request).storage
        storage.save_sources([document.source])
        storage.save_document(document)
        return _redacted(document)

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


def _optional_acquisition_runtime(request: Request) -> AcquisitionRuntime | None:
    return getattr(request.app.state, "acquisition_runtime", None)


def _acquisition_runtime(request: Request) -> AcquisitionRuntime:
    runtime = _optional_acquisition_runtime(request)
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="acquisition v1写入口未启用；旧报告与financial sync仍可用",
        )
    return runtime


def _redacted(value: Any, *, field_name: str | None = None) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        value = {item.name: getattr(value, item.name) for item in fields(value)}
    elif hasattr(value, "__dict__") and not isinstance(value, type):
        value = dict(value.__dict__)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if (
                lowered
                in {
                    "owner_token",
                    "owner_token_hash",
                    "database_path",
                    "db_path",
                    "data_root",
                }
                or is_sensitive_name(lowered)
            ):
                continue
            if lowered.endswith("absolute_path"):
                continue
            result[str(key)] = _redacted(item, field_name=lowered)
        return result
    if isinstance(value, (list, tuple)):
        return [_redacted(item, field_name=field_name) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return REDACTED_LOCAL_PATH
    if isinstance(value, str):
        if field_name and "url" in field_name and value.startswith(("http://", "https://")):
            return redact_url(value)
        if looks_like_absolute_local_path(value):
            return REDACTED_LOCAL_PATH
    return value


def _safe_error_detail(exc: Exception) -> str:
    return redact_absolute_local_paths(str(exc))


def _run_summary(repository: Any, run: Any) -> dict[str, Any]:
    payload = _redacted(run)
    events = repository.list_run_events(run.run_id)
    latest = events[-1] if events else None
    payload["status"] = (
        "planned"
        if latest is None
        else (
            latest.result.value
            if latest.event_type == AcquisitionRunEventType.FINALIZED
            and latest.result is not None
            else latest.event_type.value
        )
    )
    payload["coverage_accounted"] = (
        None if latest is None else latest.coverage_accounted
    )
    payload["material_gap_count"] = (
        None if latest is None else latest.material_gap_count
    )
    payload["default_consume_eligible"] = (
        False if latest is None else bool(latest.default_consume_eligible)
    )
    return payload


def _attempt_summary(repository: Any, attempt: Any) -> dict[str, Any]:
    payload = _redacted(attempt)
    events = repository.list_attempt_events(attempt.attempt_id)
    payload["events"] = [_redacted(item) for item in events]
    terminal = next(
        (
            item
            for item in reversed(events)
            if item.event_type.value in {"outcome_terminal", "abandoned"}
        ),
        None,
    )
    payload["status"] = (
        "started"
        if terminal is None
        else (
            terminal.outcome.value
            if terminal.outcome is not None
            else terminal.event_type.value
        )
    )
    payload["reason_code"] = None if terminal is None else terminal.reason_code
    return payload


def _plan_response(plan: Any) -> dict[str, Any]:
    return {
        "run": _redacted(plan.run),
        "run_id": plan.run.run_id,
        "physical_query_count": len(plan.physical_query_plan_items),
        "coverage_entry_count": len(plan.coverage_entries),
        "coverage_link_count": len(plan.coverage_links),
        "coverage_accounted": False,
        "material_gap_count": None,
        "checkpoint_advanced": False,
    }


class _LazyDefaultApplication:
    """Delay the legacy default app until it is actually served.

    Importing ``create_app`` for an explicitly bound acquisition server must
    not first initialize the repository-wide default SQLite/DuckDB paths or a
    second AdapterManager.  The proxy remains an ASGI application, so the
    documented ``uvicorn analysis.api:app`` entrypoint stays compatible.
    """

    def __init__(self) -> None:
        self._application: FastAPI | None = None
        self._lock = threading.Lock()

    def _resolve(self) -> FastAPI:
        application = self._application
        if application is not None:
            return application
        with self._lock:
            if self._application is None:
                self._application = create_app()
            return self._application

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._resolve()(scope, receive, send)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)


app = _LazyDefaultApplication()
