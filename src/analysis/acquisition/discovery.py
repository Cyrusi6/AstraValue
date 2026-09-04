from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import (
    DiscoveryObservation,
    DiscoveryProof,
    DiscoveredResource,
    PublishedAtPrecision,
    stable_acquisition_id,
)
from .security import ResponseSizeExceeded, sanitize_http_metadata
from .snapshots import DiscoverySnapshotRequest, FrozenSnapshotResult, SnapshotService


class DiscoveryPipelineError(RuntimeError):
    outcome = "parse_failed"

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class DiscoveryPolicySkipped(DiscoveryPipelineError):
    outcome = "policy_skipped"


class DiscoveryValidationError(DiscoveryPipelineError):
    pass


class DiscoveryCommitError(DiscoveryPipelineError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedResource:
    canonical_resource_id: str
    resource_url: str
    title: str
    source_timezone: str
    required_fetch: bool
    upstream_material_id: str | None = None
    published_at_raw: str | None = None
    published_at: datetime | None = None
    published_at_precision: PublishedAtPrecision = PublishedAtPrecision.UNKNOWN
    row_locator: str | None = None
    expected_mime_types: tuple[str, ...] = ()
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NormalizedDiscoveryPage:
    resources: tuple[NormalizedResource, ...]
    parser_id: str
    parser_version: str
    schema_id: str
    schema_version: str
    schema_valid: bool
    declared_total: int | None
    declared_page_count: int | None
    terminal: bool
    # Cursor pagination is source-owned protocol state.  Keep the adapter's
    # opaque value through validation/commit instead of inventing a page token
    # in the orchestrator.
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryPageContext:
    attempt_id: str
    physical_query_plan_item_id: str
    source_definition_id: str
    source_definition_version: str
    observed_at: datetime
    retrieved_at: datetime
    http_status: int
    mime_type: str
    page_number: int | None = None
    cursor: str | None = None
    request_summary: Mapping[str, Any] | None = None
    response_summary: Mapping[str, Any] | None = None
    observation_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryPageResult:
    observation: DiscoveryObservation
    proof: DiscoveryProof
    resources: tuple[DiscoveredResource, ...]
    discovery_snapshot_id: str | None
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryCompletion:
    total_rows: int
    page_count: int
    terminal_proven: bool
    proves_no_data: bool


class BoundedDiscoveryEnvelope:
    """A one-use in-memory body that is wiped after the non-retention transaction."""

    __slots__ = (
        "_body",
        "_discarded",
        "status_code",
        "mime_type",
        "headers",
        "sha256",
        "byte_length",
    )

    def __init__(
        self,
        body: bytes,
        *,
        max_bytes: int,
        status_code: int,
        mime_type: str,
        headers: Mapping[str, Any] | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须为正数")
        if len(body) > max_bytes:
            raise ResponseSizeExceeded("discovery body 超过固定有界内存上限")
        self._body = bytearray(body)
        self._discarded = False
        self.status_code = status_code
        self.mime_type = mime_type
        self.headers = sanitize_http_metadata(headers or {})
        self.sha256 = hashlib.sha256(body).hexdigest()
        self.byte_length = len(body)

    def read_once(self) -> bytes:
        if self._discarded:
            raise DiscoveryValidationError(
                "discovery_body_discarded", "discovery body 已按许可策略丢弃"
            )
        return bytes(self._body)

    def discard(self) -> None:
        for index in range(len(self._body)):
            self._body[index] = 0
        self._body.clear()
        self._discarded = True

    @property
    def discarded(self) -> bool:
        return self._discarded


class RetainedDiscoveryParser(Protocol):
    def parse_retained_discovery(self, snapshot_id: str) -> NormalizedDiscoveryPage: ...


class NonRetainedDiscoveryValidator(Protocol):
    def validate_and_normalize_without_retention(
        self, envelope: BoundedDiscoveryEnvelope
    ) -> NormalizedDiscoveryPage: ...


class DiscoveryRepository(Protocol):
    def commit_discovery_bundle(
        self,
        observation: DiscoveryObservation,
        proof: DiscoveryProof,
        resources: Sequence[DiscoveredResource],
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> Any: ...


class DiscoveryPipeline:
    def __init__(
        self,
        snapshot_service: SnapshotService,
        repository: DiscoveryRepository,
    ) -> None:
        self.snapshot_service = snapshot_service
        self.repository = repository

    def process_retained(
        self,
        *,
        body: bytes,
        context: DiscoveryPageContext,
        query_page_canonical: str,
        parser: RetainedDiscoveryParser,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> DiscoveryPageResult:
        frozen = self.snapshot_service.freeze_discovery_response(
            body,
            DiscoverySnapshotRequest(
                attempt_id=context.attempt_id,
                physical_query_plan_item_id=context.physical_query_plan_item_id,
                source_definition_id=context.source_definition_id,
                source_definition_version=context.source_definition_version,
                query_page_canonical=query_page_canonical,
                mime_type=context.mime_type,
                observed_at=context.observed_at,
                retrieved_at=context.retrieved_at,
                page_number=context.page_number,
                cursor=context.cursor,
                http_status=context.http_status,
                request_summary=context.request_summary,
                response_summary=context.response_summary,
                observation_id=context.observation_id,
            ),
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )
        # The parser gets only an immutable snapshot ID, never the response body.
        normalized = parser.parse_retained_discovery(frozen.snapshot.snapshot_id)
        return self._commit_normalized(
            context=context,
            normalized=normalized,
            observation=frozen.observation,
            response_sha256=frozen.snapshot.sha256,
            response_byte_length=frozen.snapshot.byte_length,
            body_retained=True,
            discovery_snapshot_id=frozen.snapshot.snapshot_id,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )

    def process_without_retention(
        self,
        *,
        envelope: BoundedDiscoveryEnvelope,
        context: DiscoveryPageContext,
        validator: NonRetainedDiscoveryValidator,
        independent_replay_required: bool,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> DiscoveryPageResult:
        if independent_replay_required:
            envelope.discard()
            raise DiscoveryPolicySkipped(
                "retention_conflicts_with_replay",
                "查询要求独立重放但许可禁止保留 discovery body",
            )
        try:
            normalized = validator.validate_and_normalize_without_retention(envelope)
            observation = DiscoveryObservation(
                observation_id=context.observation_id
                or stable_acquisition_id(
                    "discovery-observation",
                    {
                        "attempt_id": context.attempt_id,
                        "page": context.page_number,
                        "cursor": context.cursor,
                        "response_sha256": envelope.sha256,
                    },
                ),
                attempt_id=context.attempt_id,
                physical_query_plan_item_id=context.physical_query_plan_item_id,
                source_definition_id=context.source_definition_id,
                source_definition_version=context.source_definition_version,
                page_number=context.page_number,
                cursor=context.cursor,
                observed_at=_aware_utc(context.observed_at),
                retrieved_at=_aware_utc(context.retrieved_at),
                http_status=context.http_status,
                mime_type=context.mime_type,
                response_sha256=envelope.sha256,
                response_byte_length=envelope.byte_length,
                snapshot_id=None,
                request_summary=sanitize_http_metadata(context.request_summary or {}),
                response_summary=sanitize_http_metadata(context.response_summary or {}),
            )
            return self._commit_normalized(
                context=context,
                normalized=normalized,
                observation=observation,
                response_sha256=envelope.sha256,
                response_byte_length=envelope.byte_length,
                body_retained=False,
                discovery_snapshot_id=None,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
        finally:
            envelope.discard()

    def _commit_normalized(
        self,
        *,
        context: DiscoveryPageContext,
        normalized: NormalizedDiscoveryPage,
        observation: DiscoveryObservation,
        response_sha256: str,
        response_byte_length: int,
        body_retained: bool,
        discovery_snapshot_id: str | None,
        owner_token: str | None,
        lease_epoch: int | None,
    ) -> DiscoveryPageResult:
        _validate_normalized_page(normalized)
        proof_identity = {
            "observation_id": observation.observation_id,
            "response_sha256": response_sha256,
            "parser_id": normalized.parser_id,
            "parser_version": normalized.parser_version,
            "schema_id": normalized.schema_id,
            "schema_version": normalized.schema_version,
        }
        proof = DiscoveryProof(
            proof_id=stable_acquisition_id("discovery-proof", proof_identity),
            observation_id=observation.observation_id,
            attempt_id=context.attempt_id,
            physical_query_plan_item_id=context.physical_query_plan_item_id,
            response_sha256=response_sha256,
            response_byte_length=response_byte_length,
            http_status=context.http_status,
            mime_type=context.mime_type,
            parser_id=normalized.parser_id,
            parser_version=normalized.parser_version,
            schema_id=normalized.schema_id,
            schema_version=normalized.schema_version,
            schema_valid=normalized.schema_valid,
            page_number=context.page_number,
            cursor=context.cursor,
            declared_total=normalized.declared_total,
            declared_page_count=normalized.declared_page_count,
            normalized_row_count=len(normalized.resources),
            terminal=normalized.terminal,
            body_retained=body_retained,
            replayable=body_retained,
            discovery_snapshot_id=discovery_snapshot_id,
            created_at=_aware_utc(context.retrieved_at),
        )
        resources = tuple(
            _build_discovered_resource(context, proof, row, ordinal)
            for ordinal, row in enumerate(normalized.resources)
        )
        try:
            self.repository.commit_discovery_bundle(
                observation,
                proof,
                resources,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
        except Exception as exc:
            raise DiscoveryCommitError(
                "discovery_atomic_commit_failed",
                "DiscoveryObservation/Proof/Resources 原子提交失败",
            ) from exc
        return DiscoveryPageResult(
            observation=observation,
            proof=proof,
            resources=resources,
            discovery_snapshot_id=discovery_snapshot_id,
            next_cursor=normalized.next_cursor,
        )


def validate_discovery_proof_set(
    proofs: Sequence[DiscoveryProof],
) -> DiscoveryCompletion:
    if not proofs:
        raise DiscoveryValidationError("missing_page_proof", "缺少 discovery page proof")
    ordered = sorted(
        proofs,
        key=lambda item: (
            item.page_number if item.page_number is not None else 10**9,
            item.cursor or "",
            item.proof_id,
        ),
    )
    attempts = {item.attempt_id for item in ordered}
    plans = {item.physical_query_plan_item_id for item in ordered}
    if len(attempts) != 1 or len(plans) != 1:
        raise DiscoveryValidationError(
            "proof_scope_mismatch", "proof 不属于同一 attempt/physical plan item"
        )
    if any(not item.schema_valid for item in ordered):
        raise DiscoveryValidationError("schema_invalid", "存在未通过 schema 的 page proof")
    page_numbers = [item.page_number for item in ordered if item.page_number is not None]
    if page_numbers:
        if len(page_numbers) != len(ordered) or page_numbers != list(
            range(1, len(ordered) + 1)
        ):
            raise DiscoveryValidationError("missing_page_proof", "page proof 不连续")
    cursors = [item.cursor for item in ordered if item.cursor is not None]
    if cursors and len(cursors) != len(set(cursors)):
        raise DiscoveryValidationError("duplicate_cursor", "cursor proof 重复")
    terminal_indexes = [index for index, item in enumerate(ordered) if item.terminal]
    if terminal_indexes != [len(ordered) - 1]:
        raise DiscoveryValidationError(
            "terminal_proof_missing", "必须且只能由最后一页证明终止"
        )
    if not _declared_page_count_matches(ordered):
        raise DiscoveryValidationError(
            "page_count_mismatch", "声明页数与已提交 proof 不闭合"
        )
    total_rows = sum(item.normalized_row_count for item in ordered)
    declared_totals = {
        item.declared_total for item in ordered if item.declared_total is not None
    }
    if len(declared_totals) > 1 or (
        declared_totals and next(iter(declared_totals)) != total_rows
    ):
        raise DiscoveryValidationError(
            "total_mismatch", "声明总数与规范化资源数量不闭合"
        )
    proves_no_data = bool(
        total_rows == 0
        and declared_totals == {0}
        and ordered[-1].terminal
        and all(item.schema_valid for item in ordered)
    )
    return DiscoveryCompletion(
        total_rows=total_rows,
        page_count=len(ordered),
        terminal_proven=True,
        proves_no_data=proves_no_data,
    )


def no_data_is_proven(proofs: Sequence[DiscoveryProof]) -> bool:
    return validate_discovery_proof_set(proofs).proves_no_data


def _declared_page_count_matches(proofs: Sequence[DiscoveryProof]) -> bool:
    """Validate upstream page counts against physical response proofs.

    Some list APIs declare ``pageCount=0`` for a successful zero-row query.
    The client still had to receive and freeze one terminal response to prove
    that fact, so zero declared result pages legitimately close with exactly
    one empty terminal proof.  Every non-empty result keeps the ordinary
    one-proof-per-declared-page rule.
    """

    declared = {
        item.declared_page_count
        for item in proofs
        if item.declared_page_count is not None
    }
    if not declared:
        return True
    if len(declared) != 1:
        return False
    declared_count = next(iter(declared))
    if declared_count == len(proofs):
        return True
    return bool(
        declared_count == 0
        and len(proofs) == 1
        and proofs[0].terminal
        and proofs[0].normalized_row_count == 0
        and proofs[0].declared_total == 0
    )


def _validate_normalized_page(page: NormalizedDiscoveryPage) -> None:
    if not page.schema_valid:
        raise DiscoveryValidationError("schema_invalid", "discovery schema 校验失败")
    if page.declared_total is not None and page.declared_total < 0:
        raise DiscoveryValidationError("total_invalid", "declared_total 不能为负数")
    if page.declared_page_count is not None and page.declared_page_count < 0:
        raise DiscoveryValidationError(
            "page_count_invalid", "declared_page_count 不能为负数"
        )


def _build_discovered_resource(
    context: DiscoveryPageContext,
    proof: DiscoveryProof,
    row: NormalizedResource,
    ordinal: int,
) -> DiscoveredResource:
    row_payload = {
        "canonical_resource_id": row.canonical_resource_id,
        "resource_url": row.resource_url,
        "title": row.title,
        "upstream_material_id": row.upstream_material_id,
        "published_at_raw": row.published_at_raw,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "published_at_precision": row.published_at_precision.value,
        "row_locator": row.row_locator,
        "metadata": dict(row.metadata or {}),
        "ordinal": ordinal,
    }
    row_hash = hashlib.sha256(
        repr(sorted(row_payload.items())).encode("utf-8")
    ).hexdigest()
    identity = {
        "proof_id": proof.proof_id,
        "canonical_resource_id": row.canonical_resource_id,
        "row_hash": row_hash,
    }
    return DiscoveredResource(
        discovered_resource_id=stable_acquisition_id("discovered-resource", identity),
        proof_id=proof.proof_id,
        discovery_observation_id=proof.observation_id,
        discovery_attempt_id=context.attempt_id,
        source_definition_id=context.source_definition_id,
        source_definition_version=context.source_definition_version,
        canonical_resource_id=row.canonical_resource_id,
        upstream_material_id=row.upstream_material_id,
        resource_url=row.resource_url,
        title=row.title,
        published_at_raw=row.published_at_raw,
        published_at=row.published_at,
        published_at_precision=row.published_at_precision,
        source_timezone=row.source_timezone,
        page_number=context.page_number,
        cursor=context.cursor,
        row_locator=row.row_locator,
        row_hash=row_hash,
        required_fetch=row.required_fetch,
        expected_mime_types=row.expected_mime_types,
        metadata=dict(row.metadata or {}),
    )


def _aware_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
