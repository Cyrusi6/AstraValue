from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import fitz

from .models import DocumentIngestRequest, DocumentRecord, SourceRecord
from .registry import PROJECT_ROOT

if TYPE_CHECKING:
    from .acquisition.runtime import AcquisitionRuntime
    from .acquisition.snapshots import SnapshotService


RAW_ROOT = PROJECT_ROOT / "var" / "raw"
MAX_OFFICIAL_DOCUMENT_BYTES = 80 * 1024 * 1024


class SourceReviewRequired(RuntimeError):
    """The submitted URL is not evidence until a new registry version approves it."""

    def __init__(self, candidate_id: str) -> None:
        self.candidate_id = candidate_id
        super().__init__(f"来源尚未批准，已创建待人工审核 candidate: {candidate_id}")


def ingest_document(
    request: DocumentIngestRequest,
    raw_root: Path | str = RAW_ROOT,
    *,
    snapshot_service: "SnapshotService | None" = None,
    source_definition_id: str | None = None,
    source_definition_version: str | None = None,
    canonical_resource_id: str | None = None,
    upstream_material_id: str | None = None,
) -> DocumentRecord:
    source_path = Path(request.path).expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(f"文件不存在: {source_path}")
    if source_path.suffix.lower() not in {".pdf", ".html", ".htm", ".txt", ".md"}:
        raise ValueError("只支持PDF、HTML、TXT和Markdown")
    content = source_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if snapshot_service is not None:
        source_definition_id = source_definition_id or request.source_definition_id
        source_definition_version = (
            source_definition_version or request.source_definition_version
        )
        canonical_resource_id = canonical_resource_id or request.canonical_resource_id
        upstream_material_id = upstream_material_id or request.upstream_material_id
        if not request.source_url:
            raise ValueError("snapshot-backed 手工 ingest 必须提供已批准 HTTPS 来源 URL")
        return _ingest_snapshot_backed_document(
            ticker=request.ticker,
            content=content,
            title=request.title,
            source_name=request.source_name,
            source_url=request.source_url,
            published_at=request.published_at,
            provider="manual",
            announcement_id=canonical_resource_id or f"manual-{digest}",
            suffix=source_path.suffix.lower(),
            snapshot_service=snapshot_service,
            source_definition_id=source_definition_id,
            source_definition_version=source_definition_version,
            canonical_resource_id=canonical_resource_id or f"manual:{request.ticker}:{digest}",
            upstream_material_id=upstream_material_id or f"manual:{digest}",
            immutable_version_proven=False,
            metadata={"manual_ingest": True},
        )
    ticker = "".join(char for char in request.ticker if char.isalnum() or char in "-_")
    if not ticker:
        raise ValueError("股票代码不能清洗为空")
    destination_dir = Path(raw_root).resolve() / ticker
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{digest[:16]}{source_path.suffix.lower()}"
    if not destination.exists():
        shutil.copy2(source_path, destination)
    text_path = destination.with_suffix(destination.suffix + ".txt")
    text, pages, ocr_used, warnings = _extract_text(destination)
    text_path.write_text(text, encoding="utf-8")
    source = SourceRecord(
        name=request.source_name,
        source_type="official-document",
        upstream_source_id=f"official:{digest}",
        url=request.source_url,
        published_at=request.published_at,
        document_hash=digest,
        authority_level=1,
        notes=request.source_name,
    )
    return DocumentRecord(
        ticker=request.ticker,
        title=request.title,
        archived_path=str(destination),
        text_path=str(text_path),
        sha256=digest,
        source=source,
        page_count=pages,
        ocr_used=ocr_used,
        warnings=warnings,
    )


def ingest_registered_document(
    request: DocumentIngestRequest,
    runtime: "AcquisitionRuntime",
    *,
    source_definition_id: str | None = None,
    source_definition_version: str | None = None,
    canonical_resource_id: str | None = None,
    upstream_material_id: str | None = None,
) -> DocumentRecord:
    """Ingest a manual document through a frozen, approved source definition.

    This compatibility entrypoint performs no network access. It creates a
    minimal ad-hoc discovery/fetch lineage so the normal SQLite foreign keys,
    lease fencing, observations and immutable snapshot path remain in force.
    """

    from .acquisition.models import PolicyDecision
    from .acquisition.registry import SourceRegistryError
    from .acquisition.security import redact_url

    source_definition_id = source_definition_id or request.source_definition_id
    source_definition_version = (
        source_definition_version or request.source_definition_version
    )
    canonical_resource_id = canonical_resource_id or request.canonical_resource_id
    upstream_material_id = upstream_material_id or request.upstream_material_id
    if not request.source_url:
        raise SourceRegistryError(
            "启用 acquisition runtime 时，手工文档必须提供已注册 HTTPS 来源 URL"
        )
    submitted_url = request.source_url
    parsed = urlsplit(submitted_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise SourceRegistryError("手工文档来源 URL 必须是无凭据、无 fragment 的 HTTPS URL")
    try:
        if parsed.port not in (None, 443):
            raise SourceRegistryError("手工文档来源 URL 仅允许 HTTPS 443 端口")
    except ValueError as exc:
        raise SourceRegistryError("手工文档来源 URL 端口无效") from exc
    safe_url = redact_url(submitted_url)
    definition = _resolve_registered_source_definition(
        runtime,
        safe_url,
        source_definition_id=source_definition_id,
        source_definition_version=source_definition_version,
        request=request,
    )
    if definition.retention_policy.content_body != PolicyDecision.ALLOWED:
        raise SourceRegistryError("来源版本不允许保留 content body")
    if definition.license_policy.archive_original != PolicyDecision.ALLOWED:
        raise SourceRegistryError("来源版本许可不允许归档原文")
    if definition.license_policy.save_derived_text != PolicyDecision.ALLOWED:
        raise SourceRegistryError("来源版本许可不允许保存派生文本")

    source_path = Path(request.path).expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(f"文件不存在: {source_path}")
    suffix = source_path.suffix.lower()
    if suffix not in {".pdf", ".html", ".htm", ".txt", ".md"}:
        raise ValueError("只支持PDF、HTML、TXT和Markdown")
    content = source_path.read_bytes()
    size_limit = min(
        MAX_OFFICIAL_DOCUMENT_BYTES,
        int(definition.response_limits.max_response_bytes),
    )
    if len(content) > size_limit:
        raise ValueError(f"手工文档超过来源版本允许的 {size_limit} 字节上限")
    if suffix == ".pdf" and not content.startswith(b"%PDF"):
        raise ValueError("PDF扩展名文件不具有PDF magic bytes")

    digest = hashlib.sha256(content).hexdigest()
    canonical_id = canonical_resource_id or _registered_manual_resource_id(
        definition.source_definition_id,
        safe_url,
    )
    upstream_id = upstream_material_id or canonical_id
    audit = _create_registered_ingest_audit(
        runtime,
        request=request,
        definition=definition,
        source_url=safe_url,
        canonical_resource_id=canonical_id,
        upstream_material_id=upstream_id,
        content_sha256=digest,
        byte_length=len(content),
        mime_type=_mime_for_suffix(suffix),
    )
    existing = runtime.repository.find_raw_resource_snapshot(
        resource_role="content",
        source_definition_id=definition.source_definition_id,
        source_definition_version=definition.version,
        canonical_resource_id=canonical_id,
        sha256=digest,
    )
    try:
        record = _ingest_snapshot_backed_document(
            ticker=request.ticker,
            content=content,
            title=request.title,
            source_name=definition.display_name,
            source_url=safe_url,
            published_at=request.published_at,
            provider=definition.adapter_key,
            announcement_id=canonical_id,
            suffix=suffix,
            snapshot_service=runtime.snapshot_service,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            canonical_resource_id=canonical_id,
            upstream_material_id=upstream_id,
            immutable_version_proven=False,
            attempt_id=audit["fetch_attempt_id"],
            discovered_resource_id=audit["discovered_resource_id"],
            parent_discovery_attempt_id=audit["discovery_attempt_id"],
            authority_level=definition.authority_level,
            metadata={"manual_ingest": True},
        )
    except Exception as exc:
        _close_registered_ingest_audit(
            runtime,
            audit,
            outcome="parse_failed",
            reason_code=getattr(exc, "reason_code", "manual_document_ingest_failed"),
            succeeded=False,
            best_effort=True,
        )
        raise
    _close_registered_ingest_audit(
        runtime,
        audit,
        outcome=("unchanged" if existing is not None else "success"),
        reason_code=None,
        succeeded=True,
    )
    return record


def ingest_downloaded_document(
    *,
    ticker: str,
    content: bytes,
    title: str,
    source_name: str,
    source_url: str,
    published_at: datetime,
    provider: str,
    announcement_id: str,
    raw_root: Path | str = RAW_ROOT,
    metadata: dict[str, Any] | None = None,
    snapshot_service: "SnapshotService | None" = None,
    source_definition_id: str | None = None,
    source_definition_version: str | None = None,
    canonical_resource_id: str | None = None,
    upstream_material_id: str | None = None,
    immutable_version_proven: bool = True,
    attempt_id: str | None = None,
    discovered_resource_id: str | None = None,
    parent_discovery_attempt_id: str | None = None,
) -> DocumentRecord:
    """Archive a downloaded official PDF and create a deterministic evidence record."""

    if not content.startswith(b"%PDF"):
        raise ValueError("正式公告下载结果不是PDF")
    if len(content) > MAX_OFFICIAL_DOCUMENT_BYTES:
        raise ValueError(f"正式公告超过{MAX_OFFICIAL_DOCUMENT_BYTES // 1024 // 1024}MB安全上限")
    if snapshot_service is not None:
        return _ingest_snapshot_backed_document(
            ticker=ticker,
            content=content,
            title=title,
            source_name=source_name,
            source_url=source_url,
            published_at=published_at,
            provider=provider,
            announcement_id=str(announcement_id),
            suffix=".pdf",
            snapshot_service=snapshot_service,
            source_definition_id=source_definition_id,
            source_definition_version=source_definition_version,
            canonical_resource_id=canonical_resource_id or str(announcement_id),
            upstream_material_id=upstream_material_id or str(announcement_id),
            immutable_version_proven=immutable_version_proven,
            attempt_id=attempt_id,
            discovered_resource_id=discovered_resource_id,
            parent_discovery_attempt_id=parent_discovery_attempt_id,
            metadata=metadata,
        )
    digest = hashlib.sha256(content).hexdigest()
    safe_ticker = "".join(char for char in ticker if char.isalnum() or char in "-_")
    if not safe_ticker:
        raise ValueError("股票代码不能清洗为空")
    destination_dir = Path(raw_root).resolve() / safe_ticker
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{digest[:16]}.pdf"
    if not destination.exists():
        destination.write_bytes(content)
    text_path = destination.with_suffix(".pdf.txt")
    text, pages, ocr_used, warnings = _extract_text(destination)
    if not text_path.exists() or text_path.read_text(encoding="utf-8", errors="replace") != text:
        text_path.write_text(text, encoding="utf-8")
    archived_at = datetime.fromtimestamp(destination.stat().st_mtime, tz=timezone.utc)
    evidence_metadata = {
        "evidence_record_schema": "2",
        "provider": provider,
        "announcement_id": str(announcement_id),
        **(metadata or {}),
    }
    record_digest = _official_evidence_record_digest(
        ticker=ticker,
        content_digest=digest,
        title=title,
        source_name=source_name,
        source_url=source_url,
        published_at=published_at,
        provider=provider,
        announcement_id=str(announcement_id),
        metadata=evidence_metadata,
    )
    source = SourceRecord(
        source_id=f"src-official-v2-{provider}-{record_digest[:20]}",
        name=source_name,
        source_type="official-document",
        upstream_source_id=f"official-document:{digest}",
        url=source_url,
        published_at=published_at,
        retrieved_at=archived_at,
        document_hash=digest,
        authority_level=1,
        notes=title,
        metadata=evidence_metadata,
    )
    return DocumentRecord(
        document_id=f"doc-{safe_ticker}-v2-{provider}-{record_digest[:16]}",
        ticker=ticker,
        title=title,
        archived_path=str(destination),
        text_path=str(text_path),
        sha256=digest,
        source=source,
        page_count=pages,
        ocr_used=ocr_used,
        warnings=warnings,
        extracted_at=archived_at,
        metadata=evidence_metadata,
    )


def _ingest_snapshot_backed_document(
    *,
    ticker: str,
    content: bytes,
    title: str,
    source_name: str,
    source_url: str,
    published_at: datetime | None,
    provider: str,
    announcement_id: str,
    suffix: str,
    snapshot_service: "SnapshotService",
    source_definition_id: str | None,
    source_definition_version: str | None,
    canonical_resource_id: str,
    upstream_material_id: str,
    immutable_version_proven: bool,
    authority_level: int = 1,
    attempt_id: str | None = None,
    discovered_resource_id: str | None = None,
    parent_discovery_attempt_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DocumentRecord:
    """Freeze exact bytes before invoking any parser or text extractor."""

    from .acquisition.models import PublishedAtPrecision
    from .acquisition.snapshots import ContentSnapshotRequest

    if not source_definition_id or not source_definition_version:
        raise ValueError("snapshot-backed ingest 必须引用已批准 SourceDefinition 版本")
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(content).hexdigest()
    safe_ticker = "".join(char for char in ticker if char.isalnum() or char in "-_")
    if not safe_ticker:
        raise ValueError("股票代码不能清洗为空")
    identity = hashlib.sha256(
        f"{source_definition_id}|{canonical_resource_id}|{digest}".encode("utf-8")
    ).hexdigest()[:24]
    frozen = snapshot_service.freeze_content(
        content,
        ContentSnapshotRequest(
            attempt_id=attempt_id or f"compat-fetch-{identity}",
            discovered_resource_id=discovered_resource_id or f"compat-resource-{identity}",
            parent_discovery_attempt_id=parent_discovery_attempt_id
            or f"compat-discovery-{identity}",
            source_definition_id=source_definition_id,
            source_definition_version=source_definition_version,
            canonical_resource_id=canonical_resource_id,
            upstream_material_id=upstream_material_id,
            canonical_url=source_url,
            mime_type=_mime_for_suffix(suffix),
            observed_at=now,
            retrieved_at=now,
            published_at_raw=(published_at.isoformat() if published_at else None),
            published_at=published_at,
            published_at_precision=(
                PublishedAtPrecision.INSTANT
                if published_at is not None
                else PublishedAtPrecision.UNKNOWN
            ),
            immutable_version_proven=immutable_version_proven,
            original_url=source_url,
            final_url=source_url,
            request_summary={"method": "manual" if provider == "manual" else "GET"},
            response_summary={"content_type": _mime_for_suffix(suffix)},
        ),
    )
    # Parsing begins only after blob publication, re-hash and snapshot commit.
    archived_path = snapshot_service.blob_store.resolve_blob(
        frozen.snapshot.archive_relative_path
    )
    text, pages, ocr_used, warnings = _extract_text(archived_path, suffix)
    derived = snapshot_service.freeze_derived_artifact(
        parent_snapshot_id=frozen.snapshot.snapshot_id,
        artifact_type="ocr" if ocr_used else "text",
        extractor_id="astravalue-document-text",
        extractor_version="1.0.0",
        parameters={"source_suffix": suffix, "page_count": pages},
        output=text.encode("utf-8"),
    )
    text_path = snapshot_service.blob_store.data_root / Path(
        *derived.archive_relative_path.split("/")
    )
    evidence_metadata = {
        "evidence_record_schema": "3",
        "provider": provider,
        "announcement_id": announcement_id,
        "raw_resource_snapshot_id": frozen.snapshot.snapshot_id,
        "derived_artifact_id": derived.derived_artifact_id,
        **(metadata or {}),
    }
    record_digest = _official_evidence_record_digest(
        ticker=ticker,
        content_digest=digest,
        title=title,
        source_name=source_name,
        source_url=source_url,
        published_at=published_at or frozen.snapshot.created_at,
        provider=provider,
        announcement_id=announcement_id,
        metadata=evidence_metadata,
    )
    source = SourceRecord(
        source_id=f"src-snapshot-{provider}-{record_digest[:20]}",
        name=source_name,
        source_type="official-document",
        upstream_source_id=f"official-material:{upstream_material_id}",
        url=source_url,
        published_at=published_at,
        retrieved_at=frozen.snapshot.created_at,
        document_hash=digest,
        authority_level=authority_level,
        source_definition_id=source_definition_id,
        source_definition_version=source_definition_version,
        raw_resource_snapshot_id=frozen.snapshot.snapshot_id,
        canonical_resource_id=canonical_resource_id,
        available_at=frozen.snapshot.available_at,
        notes=title,
        metadata=evidence_metadata,
    )
    supersedes_document_id = (
        None
        if frozen.snapshot.supersedes_snapshot_id is None
        else f"doc-{safe_ticker}-snapshot-{frozen.snapshot.supersedes_snapshot_id}"
    )
    return DocumentRecord(
        document_id=f"doc-{safe_ticker}-snapshot-{frozen.snapshot.snapshot_id}",
        ticker=ticker,
        title=title,
        archived_path=str(archived_path),
        text_path=str(text_path),
        sha256=digest,
        source=source,
        raw_resource_snapshot_id=frozen.snapshot.snapshot_id,
        derived_artifact_id=derived.derived_artifact_id,
        supersedes_document_id=supersedes_document_id,
        document_version=frozen.snapshot.version,
        page_count=pages,
        ocr_used=ocr_used,
        warnings=warnings,
        extracted_at=derived.created_at,
        metadata=evidence_metadata,
    )


def _resolve_registered_source_definition(
    runtime: "AcquisitionRuntime",
    source_url: str,
    *,
    source_definition_id: str | None,
    source_definition_version: str | None,
    request: DocumentIngestRequest,
):
    from .acquisition.registry import (
        SourceRegistryError,
        SourceRegistryLoader,
        create_source_candidate,
    )
    from .acquisition.repository import AcquisitionNotFoundError
    from .acquisition.runtime import infer_a_share_market

    registry = runtime.loaded_registry.registry
    definitions = tuple(registry.definitions)
    if source_definition_version is not None and source_definition_id is None:
        raise SourceRegistryError("指定来源版本时必须同时提供 source_definition_id")

    selected = None
    if source_definition_id is not None:
        direct = [
            item
            for item in definitions
            if item.source_definition_id == source_definition_id
        ]
        if direct:
            if source_definition_version is not None:
                direct = [
                    item
                    for item in direct
                    if str(item.version) == str(source_definition_version)
                ]
                if not direct:
                    raise SourceRegistryError(
                        f"来源版本不在当前冻结注册表: {source_definition_id}@{source_definition_version}"
                    )
            candidates = tuple(direct)
        else:
            if source_definition_version is not None:
                raise SourceRegistryError("alias 不能与 source_definition_version 组合使用")
            candidates = SourceRegistryLoader.resolve_alias(
                registry,
                source_definition_id,
                scope="business_model",
            )
        matching = [
            item
            for item in candidates
            if SourceRegistryLoader.is_formal_evidence_definition_for_url(
                item,
                source_url,
            )
        ]
        if len(matching) == 1:
            selected = matching[0]
        elif len(matching) > 1:
            raise SourceRegistryError("来源 alias 对提交 URL 的解析结果不唯一")
        else:
            registered = SourceRegistryLoader.find_registered_definition_for_url(
                registry, source_url
            )
            if registered is not None:
                raise SourceRegistryError(
                    "提交 URL 属于另一已批准来源，不能覆盖显式 source_definition_id/alias"
                )
    else:
        selected = SourceRegistryLoader.find_registered_definition_for_url(
            registry, source_url
        )

    if selected is None:
        candidate = create_source_candidate(
            runtime.loaded_registry,
            url=source_url,
            discovery_context={
                "entrypoint": "manual_document_ingest",
                "ticker": request.ticker,
                "submitted_source_name": request.source_name,
            },
            suggested_upstream_identity=source_definition_id,
        )
        try:
            existing = runtime.repository.get_source_candidate(candidate.candidate_id)
        except AcquisitionNotFoundError:
            runtime.repository.save_source_candidate(candidate)
        else:
            candidate = existing
        raise SourceReviewRequired(candidate.candidate_id)

    now = datetime.now(timezone.utc)
    SourceRegistryLoader.assert_effective(selected, now)
    if (
        not SourceRegistryLoader.is_approved_for_formal_evidence(selected)
        or "business_model" not in selected.scopes
    ):
        raise SourceRegistryError("来源不是当前启用且已批准的 business_model v1 正式来源")
    try:
        market = infer_a_share_market(request.ticker)
    except ValueError as exc:
        raise SourceRegistryError(str(exc)) from exc
    if not selected.applies_to(request.ticker, market):
        raise SourceRegistryError("来源版本不适用于该公司/市场")
    return selected


def _registered_manual_resource_id(source_definition_id: str, source_url: str) -> str:
    from .acquisition.models import stable_acquisition_id

    return stable_acquisition_id(
        "manual-resource",
        {
            "source_definition_id": source_definition_id,
            "canonical_url": source_url,
        },
    )


def _create_registered_ingest_audit(
    runtime: "AcquisitionRuntime",
    *,
    request: DocumentIngestRequest,
    definition,
    source_url: str,
    canonical_resource_id: str,
    upstream_material_id: str,
    content_sha256: str,
    byte_length: int,
    mime_type: str,
) -> dict[str, Any]:
    from .acquisition.models import (
        AcquisitionAttempt,
        AcquisitionAttemptEvent,
        AcquisitionAttemptEventType,
        AcquisitionMode,
        AcquisitionOutcome,
        AcquisitionRun,
        AcquisitionRunEvent,
        AcquisitionRunEventType,
        AcquisitionRunKind,
        AttemptKind,
        DiscoveredResource,
        DiscoveryObservation,
        DiscoveryProof,
        FetchPolicy,
        PhysicalQueryPlanItem,
        PublishedAtPrecision,
        SourceDefinitionRef,
        acquisition_new_id,
        canonical_json_bytes,
        canonical_json_sha256,
    )
    from .acquisition.registry import SourceRegistryError

    query_candidates = [
        item
        for item in definition.queries
        if item.fetch_policy == FetchPolicy.REQUIRED_ATTACHMENT and item.endpoint is not None
    ]
    if not query_candidates:
        raise SourceRegistryError("来源版本没有允许正式附件入库的 registry query")
    query = next(
        (item for item in query_candidates if item.query_family == "business_announcement"),
        query_candidates[0],
    )
    now = datetime.now(timezone.utc)
    run_id = acquisition_new_id()
    run = AcquisitionRun(
        run_id=run_id,
        ticker=request.ticker,
        company_name=request.ticker,
        mode=AcquisitionMode.BASELINE,
        run_kind=AcquisitionRunKind.AD_HOC,
        as_of=now,
        created_at=now,
        registry_id=runtime.loaded_registry.registry.registry_id,
        registry_version=runtime.loaded_registry.registry.registry_version,
        registry_content_hash=runtime.loaded_registry.content_hash,
        question_set_id=runtime.loaded_questions.question_set.question_set_id,
        question_set_version=runtime.loaded_questions.question_set.version,
        question_set_content_hash=runtime.loaded_questions.content_hash,
        source_definition_refs=(
            SourceDefinitionRef(
                source_definition_id=definition.source_definition_id,
                version=definition.version,
                content_hash=runtime.loaded_registry.source_definition_hashes[
                    (definition.source_definition_id, definition.version)
                ],
            ),
        ),
        request_scope="ad_hoc",
        storage_namespace_id=runtime.namespace_id,
    )
    discovery_plan_id = acquisition_new_id()
    discovery_plan = PhysicalQueryPlanItem(
        plan_item_id=discovery_plan_id,
        run_id=run_id,
        source_definition_id=definition.source_definition_id,
        source_definition_version=definition.version,
        query_id=query.query_id,
        query_family=query.query_family,
        execution_key=f"{query.execution_key}:manual:{run_id}",
        attempt_kind=AttemptKind.DISCOVERY,
        request_method=query.request_method,
        endpoint=query.endpoint,
        normalized_parameters={"entrypoint": "manual_document_ingest"},
        partition_key=f"{query.partition_key}:manual",
        pagination_fingerprint=canonical_json_sha256(query.pagination),
        ordinal=0,
        time_start=now - timedelta(microseconds=1),
        time_end=now,
    )
    runtime.repository.save_plan_bundle(run, (discovery_plan,), (), ())
    lease, owner_token = runtime.repository.claim_lease(run_id, ttl_seconds=3600)
    audit: dict[str, Any] = {
        "run_id": run_id,
        "owner_token": owner_token,
        "lease_epoch": lease.lease_epoch,
    }
    try:
        runtime.repository.append_run_event(
            AcquisitionRunEvent(
                run_id=run_id,
                event_type=AcquisitionRunEventType.RUNNING,
                occurred_at=now,
                lease_epoch=lease.lease_epoch,
                metadata={"entrypoint": "manual_document_ingest"},
            ),
            owner_token=owner_token,
        )
        discovery_attempt_id = acquisition_new_id()
        discovery_attempt = AcquisitionAttempt(
            attempt_id=discovery_attempt_id,
            run_id=run_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            physical_query_plan_item_id=discovery_plan_id,
            execution_key=discovery_plan.execution_key,
            attempt_kind=AttemptKind.DISCOVERY,
            query_id=query.query_id,
            page_number=1,
            work_position="manual:page:1",
            retry_group_id=acquisition_new_id(),
            lease_epoch=lease.lease_epoch,
            request_summary={"entrypoint": "manual_document_ingest"},
            started_at=now,
        )
        runtime.repository.save_attempt(discovery_attempt, owner_token=owner_token)
        runtime.repository.append_attempt_event(
            AcquisitionAttemptEvent(
                attempt_id=discovery_attempt_id,
                event_type=AcquisitionAttemptEventType.STARTED,
                occurred_at=now,
                lease_epoch=lease.lease_epoch,
                protocol_summary={"entrypoint": "manual_document_ingest"},
            ),
            owner_token=owner_token,
        )
        discovery_payload = canonical_json_bytes(
            {
                "schema": "manual-registered-document-discovery-v1",
                "source_definition_id": definition.source_definition_id,
                "source_definition_version": definition.version,
                "canonical_resource_id": canonical_resource_id,
                "upstream_material_id": upstream_material_id,
                "source_url": source_url,
                "content_sha256": content_sha256,
                "byte_length": byte_length,
                "mime_type": mime_type,
            }
        )
        discovery_sha256 = hashlib.sha256(discovery_payload).hexdigest()
        discovery_observation_id = acquisition_new_id()
        observation = DiscoveryObservation(
            observation_id=discovery_observation_id,
            attempt_id=discovery_attempt_id,
            physical_query_plan_item_id=discovery_plan_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            page_number=1,
            observed_at=now,
            retrieved_at=now,
            http_status=200,
            mime_type="application/json",
            response_sha256=discovery_sha256,
            response_byte_length=len(discovery_payload),
            request_summary={"entrypoint": "manual_document_ingest"},
            response_summary={"schema": "manual-registered-document-discovery-v1"},
        )
        proof_id = acquisition_new_id()
        proof = DiscoveryProof(
            proof_id=proof_id,
            observation_id=discovery_observation_id,
            attempt_id=discovery_attempt_id,
            physical_query_plan_item_id=discovery_plan_id,
            response_sha256=discovery_sha256,
            response_byte_length=len(discovery_payload),
            http_status=200,
            mime_type="application/json",
            parser_id="manual-registered-document",
            parser_version="1.0.0",
            schema_id="manual-registered-document-discovery",
            schema_version="1.0.0",
            schema_valid=True,
            page_number=1,
            declared_total=1,
            declared_page_count=1,
            normalized_row_count=1,
            terminal=True,
            body_retained=False,
            replayable=False,
            created_at=now,
        )
        discovered_resource_id = acquisition_new_id()
        published_precision = (
            PublishedAtPrecision.INSTANT
            if request.published_at is not None
            else PublishedAtPrecision.UNKNOWN
        )
        resource = DiscoveredResource(
            discovered_resource_id=discovered_resource_id,
            proof_id=proof_id,
            discovery_observation_id=discovery_observation_id,
            discovery_attempt_id=discovery_attempt_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            canonical_resource_id=canonical_resource_id,
            upstream_material_id=upstream_material_id,
            resource_url=source_url,
            title=request.title,
            published_at_raw=(
                request.published_at.isoformat() if request.published_at else None
            ),
            published_at=request.published_at,
            published_at_precision=published_precision,
            source_timezone=definition.source_timezone,
            page_number=1,
            row_locator="manual-registered-document-discovery-v1:item:0",
            row_hash=canonical_json_sha256(
                {
                    "canonical_resource_id": canonical_resource_id,
                    "source_url": source_url,
                    "content_sha256": content_sha256,
                }
            ),
            required_fetch=True,
            expected_mime_types=(mime_type,),
        )
        runtime.repository.commit_discovery_bundle(
            observation,
            proof,
            (resource,),
            owner_token=owner_token,
            lease_epoch=lease.lease_epoch,
        )
        runtime.repository.append_attempt_event(
            AcquisitionAttemptEvent(
                attempt_id=discovery_attempt_id,
                event_type=AcquisitionAttemptEventType.OUTCOME_TERMINAL,
                occurred_at=datetime.now(timezone.utc),
                lease_epoch=lease.lease_epoch,
                outcome=AcquisitionOutcome.SUCCESS,
                proof_ids=(proof_id,),
                protocol_summary={"body_retained": False, "manual_submission": True},
            ),
            owner_token=owner_token,
        )

        fetch_plan_id = acquisition_new_id()
        fetch_plan = PhysicalQueryPlanItem(
            plan_item_id=fetch_plan_id,
            run_id=run_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            query_id=query.query_id,
            query_family=query.query_family,
            execution_key=f"{query.execution_key}:manual-fetch:{run_id}",
            attempt_kind=AttemptKind.FETCH,
            request_method="GET",
            endpoint=source_url,
            normalized_parameters={"entrypoint": "manual_document_ingest"},
            partition_key=f"{query.partition_key}:manual-fetch",
            pagination_fingerprint=canonical_json_sha256(
                {"strategy": "single_manual_resource", "version": 1}
            ),
            ordinal=1,
            parent_plan_item_id=discovery_plan_id,
            discovered_resource_id=discovered_resource_id,
            time_start=now - timedelta(microseconds=1),
            time_end=now,
        )
        runtime.repository.save_physical_query_plan_item(fetch_plan)
        fetch_attempt_id = acquisition_new_id()
        fetch_attempt = AcquisitionAttempt(
            attempt_id=fetch_attempt_id,
            run_id=run_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            physical_query_plan_item_id=fetch_plan_id,
            execution_key=fetch_plan.execution_key,
            attempt_kind=AttemptKind.FETCH,
            discovered_resource_id=discovered_resource_id,
            parent_discovery_attempt_id=discovery_attempt_id,
            work_position=f"manual:resource:{canonical_resource_id}",
            retry_group_id=acquisition_new_id(),
            lease_epoch=lease.lease_epoch,
            request_summary={"entrypoint": "manual_document_ingest"},
            started_at=datetime.now(timezone.utc),
        )
        runtime.repository.save_attempt(fetch_attempt, owner_token=owner_token)
        runtime.repository.append_attempt_event(
            AcquisitionAttemptEvent(
                attempt_id=fetch_attempt_id,
                event_type=AcquisitionAttemptEventType.STARTED,
                occurred_at=datetime.now(timezone.utc),
                lease_epoch=lease.lease_epoch,
                protocol_summary={"entrypoint": "manual_document_ingest"},
            ),
            owner_token=owner_token,
        )
        audit.update(
            discovery_attempt_id=discovery_attempt_id,
            fetch_attempt_id=fetch_attempt_id,
            discovered_resource_id=discovered_resource_id,
        )
        return audit
    except Exception:
        try:
            runtime.repository.release_lease(
                run_id,
                owner_token=owner_token,
                lease_epoch=lease.lease_epoch,
            )
        except Exception:
            pass
        raise


def _close_registered_ingest_audit(
    runtime: "AcquisitionRuntime",
    audit: dict[str, Any],
    *,
    outcome: str,
    reason_code: str | None,
    succeeded: bool,
    best_effort: bool = False,
) -> None:
    from .acquisition.models import (
        AcquisitionAttemptEvent,
        AcquisitionAttemptEventType,
        AcquisitionOutcome,
        AcquisitionRunEvent,
        AcquisitionRunEventType,
        AcquisitionRunResult,
    )

    repository = runtime.repository
    owner_token = audit["owner_token"]
    lease_epoch = audit["lease_epoch"]
    errors: list[Exception] = []
    try:
        repository.append_attempt_event(
            AcquisitionAttemptEvent(
                attempt_id=audit["fetch_attempt_id"],
                event_type=AcquisitionAttemptEventType.OUTCOME_TERMINAL,
                occurred_at=datetime.now(timezone.utc),
                lease_epoch=lease_epoch,
                outcome=AcquisitionOutcome(outcome),
                reason_code=reason_code,
                protocol_summary={"manual_submission": True},
            ),
            owner_token=owner_token,
        )
    except Exception as exc:
        errors.append(exc)
    try:
        repository.finalize_run(
            audit["run_id"],
            run_event=AcquisitionRunEvent(
                run_id=audit["run_id"],
                event_type=AcquisitionRunEventType.FINALIZED,
                occurred_at=datetime.now(timezone.utc),
                lease_epoch=lease_epoch,
                result=(
                    AcquisitionRunResult.SUCCEEDED
                    if succeeded
                    else AcquisitionRunResult.FAILED
                ),
                coverage_accounted=succeeded,
                material_gap_count=(0 if succeeded else 1),
                default_consume_eligible=False,
                reason_code=reason_code,
                metadata={"entrypoint": "manual_document_ingest"},
            ),
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )
    except Exception as exc:
        errors.append(exc)
    try:
        repository.release_lease(
            audit["run_id"],
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )
    except Exception as exc:
        errors.append(exc)
    if errors and not best_effort:
        raise errors[0]


def _mime_for_suffix(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".htm": "text/html",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(suffix.lower(), "application/octet-stream")


def _official_evidence_record_digest(
    *,
    ticker: str,
    content_digest: str,
    title: str,
    source_name: str,
    source_url: str,
    published_at: datetime,
    provider: str,
    announcement_id: str,
    metadata: dict[str, Any],
) -> str:
    published = (
        published_at.replace(tzinfo=timezone.utc)
        if published_at.tzinfo is None
        else published_at.astimezone(timezone.utc)
    )
    payload = {
        "schema": "official-evidence-record-v2",
        "ticker": ticker,
        "content_digest": content_digest,
        "title": title,
        "source_name": source_name,
        "source_url": source_url,
        "published_at": published.isoformat(),
        "provider": provider,
        "announcement_id": announcement_id,
        "metadata": metadata,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_text(
    path: Path,
    source_suffix: str | None = None,
) -> tuple[str, int, bool, list[str]]:
    suffix = (source_suffix or path.suffix).lower()
    if suffix == ".pdf":
        warnings: list[str] = []
        ocr_used = False
        with fitz.open(path) as document:
            pages = []
            for index, page in enumerate(document, 1):
                page_text = page.get_text("text")
                if not page_text.strip():
                    warnings.append("扫描页待通过已归档快照的 MinerU 精准解析流程补全")
                pages.append(f"--- page {index} ---\n{page_text}")
        if not any(item.split("\n", 1)[-1].strip() for item in pages):
            warnings.append("PDF未提取到原生文本，待 MinerU 精准解析")
        return "\n\n".join(pages), len(pages), ocr_used, sorted(set(warnings))
    text = path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
    return text, 1, False, []
