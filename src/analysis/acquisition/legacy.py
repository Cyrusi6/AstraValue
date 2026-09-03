from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    AvailableAtBasis,
    ContentBlob,
    PublishedAtPrecision,
    RawResourceSnapshot,
    ResourceRole,
    stable_acquisition_id,
)


@dataclass(frozen=True, slots=True)
class LegacySnapshotReconcileResult:
    document_id: str
    status: str
    reason_code: str
    snapshot_id: str | None = None
    sha256: str | None = None


def reconcile_legacy_document_snapshot(
    runtime: Any,
    document: Any,
    *,
    source_definition_id: str | None = None,
    source_definition_version: str | None = None,
) -> LegacySnapshotReconcileResult:
    """Verify one pre-v6 raw file and add an immutable legacy snapshot.

    The operation deliberately does not update the historical DocumentRecord,
    fabricate an AcquisitionRun/Attempt, or inspect ``provider_results``.  The
    returned snapshot ID can be attached only by an explicit later migration or
    new document version.
    """

    path = Path(str(document.archived_path)).expanduser()
    if not path.is_file():
        return LegacySnapshotReconcileResult(
            document_id=document.document_id,
            status="unresolved",
            reason_code="legacy_raw_missing",
        )
    expected = str(document.sha256).strip().lower()
    actual, byte_length = _hash_file(path)
    if actual != expected:
        return LegacySnapshotReconcileResult(
            document_id=document.document_id,
            status="unresolved",
            reason_code="legacy_raw_hash_mismatch",
            sha256=actual,
        )

    source = document.source
    definition_id = (
        source_definition_id
        or source.source_definition_id
        or "legacy.official"
    )
    definition_version = (
        source_definition_version
        or source.source_definition_version
        or "1.0.0"
    )
    definition = runtime.source_definition(definition_id, definition_version)
    if not definition.legacy:
        raise ValueError("legacy raw reconcile只能引用registry中的legacy定义")
    canonical_url = source.url
    if not canonical_url or not canonical_url.lower().startswith("https://"):
        return LegacySnapshotReconcileResult(
            document_id=document.document_id,
            status="unresolved",
            reason_code="legacy_canonical_url_unverifiable",
            sha256=actual,
        )

    canonical_id = (
        source.canonical_resource_id
        or f"legacy-document:{document.document_id}"
    )
    existing = runtime.repository.find_raw_resource_snapshot(
        resource_role=ResourceRole.CONTENT.value,
        source_definition_id=definition_id,
        source_definition_version=definition_version,
        canonical_resource_id=canonical_id,
        sha256=actual,
    )
    if existing is not None:
        return LegacySnapshotReconcileResult(
            document_id=document.document_id,
            status="linked",
            reason_code="legacy_raw_already_verified",
            snapshot_id=existing.snapshot_id,
            sha256=actual,
        )

    archived = runtime.blob_store.archive_chunks(
        _read_chunks(path),
        expected_sha256=expected,
        expected_length=byte_length,
    )
    blob = ContentBlob(
        content_blob_id=f"sha256:{actual}",
        storage_namespace_id=runtime.namespace_id,
        sha256=actual,
        byte_length=byte_length,
        archive_relative_path=archived.relative_path,
        created_at=archived.archived_at,
    )
    previous = runtime.repository.find_raw_resource_snapshot(
        resource_role=ResourceRole.CONTENT.value,
        source_definition_id=definition_id,
        source_definition_version=definition_version,
        canonical_resource_id=canonical_id,
    )
    version = 1 if previous is None else previous.version + 1
    # A legacy record proves that these exact bytes existed no later than the
    # recorded retrieval.  A historical publication date alone does not prove
    # that the current bytes were not silently replaced in the meantime, so it
    # must never move point-in-time eligibility backwards.
    available_at = _aware(source.available_at or source.retrieved_at)
    snapshot = RawResourceSnapshot(
        snapshot_id=stable_acquisition_id(
            "legacy-snapshot",
            {
                "namespace": runtime.namespace_id,
                "document_id": document.document_id,
                "sha256": actual,
            },
        ),
        resource_role=ResourceRole.CONTENT,
        storage_namespace_id=runtime.namespace_id,
        source_definition_id=definition_id,
        source_definition_version=definition_version,
        creating_observation_id=f"legacy-reconcile:{document.document_id}",
        content_blob_id=blob.content_blob_id,
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        byte_length=byte_length,
        sha256=actual,
        archive_relative_path=archived.relative_path,
        available_at=available_at,
        available_at_basis=AvailableAtBasis.LEGACY_VERIFIED,
        version=version,
        supersedes_snapshot_id=(None if previous is None else previous.snapshot_id),
        policy_decision=(
            "legacy_verified_only:no_attempt_or_checkpoint_inferred"
        ),
        created_at=datetime.now(timezone.utc),
        canonical_resource_id=canonical_id,
        upstream_material_id=(
            source.upstream_source_id
            or f"legacy-document:{document.document_id}"
        ),
        canonical_url=canonical_url,
        published_at_raw=(
            None if source.published_at is None else source.published_at.isoformat()
        ),
        published_at=None,
        published_at_precision=PublishedAtPrecision.UNKNOWN,
        source_timezone="Asia/Shanghai",
    )
    runtime.repository.save_content_blob(blob)
    runtime.repository.save_raw_resource_snapshot(snapshot)
    return LegacySnapshotReconcileResult(
        document_id=document.document_id,
        status="linked",
        reason_code="legacy_raw_hash_verified",
        snapshot_id=snapshot.snapshot_id,
        sha256=actual,
    )


def _read_chunks(path: Path, size: int = 1024 * 1024):
    with path.open("rb") as stream:
        while chunk := stream.read(size):
            yield chunk


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    for chunk in _read_chunks(path):
        digest.update(chunk)
        length += len(chunk)
    return digest.hexdigest(), length


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("legacy文档时间必须包含时区")
    return value.astimezone(timezone.utc)


__all__ = [
    "LegacySnapshotReconcileResult",
    "reconcile_legacy_document_snapshot",
]
