from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, TypeVar
from zoneinfo import ZoneInfo

from .models import (
    AcquisitionOutcome,
    AvailableAtBasis,
    ContentBlob,
    DerivedArtifact,
    DiscoveryObservation,
    PublishedAtPrecision,
    RawResourceSnapshot,
    ResourceDisposition,
    ResourceObservation,
    ResourceRole,
    SnapshotIntegrityEvent,
    SnapshotIntegrityStatus,
    acquisition_new_id,
    stable_acquisition_id,
)
from .security import redact_redirect_chain, sanitize_http_metadata


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ARCHIVE_WRITE_FAILED = "archive_write_failed"
INTEGRITY_MISMATCH = "integrity_mismatch"
SNAPSHOT_COMMIT_FAILED = "snapshot_commit_failed"


class SnapshotPipelineError(RuntimeError):
    """A snapshot-stage failure with the only allowed acquisition mapping."""

    outcome = "parse_failed"

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ArchiveWriteError(SnapshotPipelineError):
    def __init__(self, message: str) -> None:
        super().__init__(ARCHIVE_WRITE_FAILED, message)


class SnapshotIntegrityMismatch(SnapshotPipelineError):
    def __init__(self, message: str) -> None:
        super().__init__(INTEGRITY_MISMATCH, message)


class SnapshotCommitError(SnapshotPipelineError):
    def __init__(self, message: str, *, orphan_relative_path: str | None = None) -> None:
        super().__init__(SNAPSHOT_COMMIT_FAILED, message)
        self.orphan_relative_path = orphan_relative_path


class SnapshotPathEscape(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArchivedBlob:
    storage_namespace_id: str
    sha256: str
    byte_length: int
    relative_path: str
    archived_at: datetime
    reused: bool

    def __post_init__(self) -> None:
        if not SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("blob SHA-256 必须是完整 64 位小写十六进制")
        if self.byte_length < 0:
            raise ValueError("blob 长度不能为负数")
        _validate_relative_identifier(self.relative_path)


@dataclass(frozen=True, slots=True)
class PointInTimeDecision:
    available_at: datetime
    available_at_basis: str
    published_at: datetime | None
    published_at_precision: str
    source_timezone: str


@dataclass(frozen=True, slots=True)
class OrphanBlob:
    sha256: str
    byte_length: int
    relative_path: str


@dataclass(frozen=True, slots=True)
class ArchivedDerivedArtifact:
    storage_namespace_id: str
    parent_snapshot_id: str
    extractor_id: str
    output_sha256: str
    byte_length: int
    relative_path: str
    archived_at: datetime
    reused: bool


@dataclass(frozen=True, slots=True)
class ContentSnapshotRequest:
    attempt_id: str
    discovered_resource_id: str
    parent_discovery_attempt_id: str
    source_definition_id: str
    source_definition_version: str
    canonical_resource_id: str
    upstream_material_id: str
    canonical_url: str
    mime_type: str
    observed_at: datetime
    retrieved_at: datetime
    published_at_raw: str | None = None
    published_at: datetime | date | None = None
    published_at_precision: PublishedAtPrecision = PublishedAtPrecision.UNKNOWN
    source_timezone: str = "Asia/Shanghai"
    immutable_version_proven: bool = False
    policy_decision: str = "allowed"
    original_url: str | None = None
    final_url: str | None = None
    redirect_chain: tuple[str, ...] = ()
    request_summary: Mapping[str, Any] | None = None
    response_summary: Mapping[str, Any] | None = None
    http_status: int | None = 200
    etag: str | None = None
    last_modified: str | None = None
    validator_source_snapshot_id: str | None = None
    reason_code: str | None = None
    observation_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoverySnapshotRequest:
    attempt_id: str
    physical_query_plan_item_id: str
    source_definition_id: str
    source_definition_version: str
    query_page_canonical: str
    mime_type: str
    observed_at: datetime
    retrieved_at: datetime
    page_number: int | None = None
    cursor: str | None = None
    policy_decision: str = "allowed"
    http_status: int | None = 200
    request_summary: Mapping[str, Any] | None = None
    response_summary: Mapping[str, Any] | None = None
    observation_id: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenSnapshotResult:
    blob: ContentBlob
    snapshot: RawResourceSnapshot
    observation: ResourceObservation | DiscoveryObservation
    disposition: str
    created_snapshot: bool


@dataclass(frozen=True, slots=True)
class IntegrityCheckResult:
    event: SnapshotIntegrityEvent
    reconcile_candidate: Any | None = None


class SnapshotRepository(Protocol):
    def commit_snapshot_bundle(
        self,
        blob: Any,
        snapshot: Any,
        observation: Any,
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> Any: ...

    def save_derived_artifact(self, artifact: Any) -> Any: ...

    def find_raw_resource_snapshot(self, **filters: Any) -> Any: ...

    def save_resource_observation(self, observation: Any, **kwargs: Any) -> Any: ...

    def save_discovery_observation(self, observation: Any, **kwargs: Any) -> Any: ...

    def get_raw_resource_snapshot(self, snapshot_id: str) -> Any: ...

    def append_snapshot_integrity_event(self, event: Any) -> Any: ...

    def list_snapshot_integrity_events(self, snapshot_id: str) -> Sequence[Any]: ...

    def commit_discovery_snapshot_bundle(
        self,
        blob: Any,
        snapshot: Any,
        discovery_observation: Any,
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> Any: ...


BundleT = TypeVar("BundleT", bound=tuple[Any, Any, Any])


class ContentAddressedBlobStore:
    """Namespace-bound, content-addressed storage for immutable raw bytes.

    The caller cannot choose a target path. A complete SHA-256 always determines
    ``raw/blobs/sha256/xx/<digest>`` below the explicitly bound data root.
    """

    def __init__(
        self,
        data_root: Path | str,
        storage_namespace: Any,
        *,
        binding_validator: Callable[[Path, str], None] | None = None,
    ) -> None:
        namespace_id = _namespace_id(storage_namespace)
        root = Path(data_root).expanduser().resolve()
        if not namespace_id:
            raise ValueError("StorageNamespace 缺少 namespace_id")
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError("data_root 不是目录")
        if binding_validator is not None:
            binding_validator(root, namespace_id)
        self.data_root = root
        self.storage_namespace_id = namespace_id

    def archive_bytes(
        self,
        content: bytes | bytearray | memoryview,
        *,
        expected_sha256: str | None = None,
        expected_length: int | None = None,
    ) -> ArchivedBlob:
        return self.archive_chunks(
            (bytes(content),),
            expected_sha256=expected_sha256,
            expected_length=expected_length,
        )

    def archive_chunks(
        self,
        chunks: Iterable[bytes],
        *,
        expected_sha256: str | None = None,
        expected_length: int | None = None,
    ) -> ArchivedBlob:
        if expected_sha256 is not None:
            expected_sha256 = _validate_sha256(expected_sha256)
        if expected_length is not None and expected_length < 0:
            raise ValueError("expected_length 不能为负数")

        digest = hashlib.sha256()
        byte_length = 0
        materialized_chunks: list[bytes] = []
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("blob chunk 必须为 bytes")
            materialized_chunks.append(chunk)
            digest.update(chunk)
            byte_length += len(chunk)

        actual_sha256 = digest.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise SnapshotIntegrityMismatch("下载字节 SHA-256 与预期不一致")
        if expected_length is not None and byte_length != expected_length:
            raise SnapshotIntegrityMismatch("下载字节长度与预期不一致")

        relative_path = self.blob_relative_path(actual_sha256)
        destination = self._resolve_relative(relative_path)
        reused = destination.exists()
        temp_path = destination.parent / f".{destination.name}.{os.getpid()}-{secrets.token_hex(12)}.tmp"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._assert_resolved_inside(destination.parent)
            if not reused:
                with temp_path.open("xb") as stream:
                    for chunk in materialized_chunks:
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, destination)
                self._fsync_directory(destination.parent)
        except Exception as exc:
            _best_effort_unlink(temp_path)
            raise ArchiveWriteError("blob 原子发布失败") from exc

        try:
            verified_sha256, verified_length = _hash_file(destination)
        except OSError as exc:
            self._quarantine_if_present(destination, "post_publish_read_failed")
            raise SnapshotIntegrityMismatch("blob 发布后无法重新读取") from exc
        if verified_sha256 != actual_sha256 or verified_length != byte_length:
            self._quarantine_if_present(destination, "post_publish_integrity_mismatch")
            raise SnapshotIntegrityMismatch("blob 原子发布后完整哈希或长度不一致")
        archived_at = datetime.fromtimestamp(destination.stat().st_mtime, tz=timezone.utc)
        return ArchivedBlob(
            storage_namespace_id=self.storage_namespace_id,
            sha256=actual_sha256,
            byte_length=byte_length,
            relative_path=relative_path,
            archived_at=archived_at,
            reused=reused,
        )

    def blob_relative_path(self, sha256: str) -> str:
        digest = _validate_sha256(sha256)
        return f"raw/blobs/sha256/{digest[:2]}/{digest}"

    def resolve_blob(self, relative_path: str) -> Path:
        identifier = _validate_relative_identifier(relative_path)
        if not identifier.startswith("raw/blobs/sha256/"):
            raise SnapshotPathEscape("快照 blob 标识不属于内容寻址目录")
        return self._resolve_relative(identifier)

    def read_verified(
        self,
        relative_path: str,
        *,
        expected_sha256: str,
        expected_length: int,
    ) -> bytes:
        path = self.resolve_blob(relative_path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SnapshotIntegrityMismatch("快照归档文件缺失或不可读") from exc
        if len(content) != expected_length:
            raise SnapshotIntegrityMismatch("快照归档长度不匹配")
        if hashlib.sha256(content).hexdigest() != _validate_sha256(expected_sha256):
            raise SnapshotIntegrityMismatch("快照归档 SHA-256 不匹配")
        return content

    def verify_blob(
        self,
        relative_path: str,
        *,
        expected_sha256: str,
        expected_length: int,
    ) -> None:
        self.read_verified(
            relative_path,
            expected_sha256=expected_sha256,
            expected_length=expected_length,
        )

    def scan_orphans(self, referenced_relative_paths: Iterable[str]) -> tuple[OrphanBlob, ...]:
        """Report unreferenced complete blobs; never delete or move them."""

        referenced = {
            _validate_relative_identifier(item) for item in referenced_relative_paths
        }
        base = self._resolve_relative("raw/blobs/sha256")
        if not base.exists():
            return ()
        orphans: list[OrphanBlob] = []
        for path in sorted(base.glob("[0-9a-f][0-9a-f]/" + "[0-9a-f]" * 64)):
            if not path.is_file():
                continue
            relative = path.relative_to(self.data_root).as_posix()
            if relative in referenced:
                continue
            digest, length = _hash_file(path)
            orphans.append(
                OrphanBlob(
                    sha256=digest,
                    byte_length=length,
                    relative_path=relative,
                )
            )
        return tuple(orphans)

    def archive_derived_bytes(
        self,
        *,
        parent_snapshot_id: str,
        extractor_id: str,
        output: bytes,
    ) -> ArchivedDerivedArtifact:
        snapshot_component = _safe_component(parent_snapshot_id, "parent_snapshot_id")
        extractor_component = _safe_component(extractor_id, "extractor_id")
        digest = hashlib.sha256(output).hexdigest()
        relative_path = (
            f"raw/derived/{snapshot_component}/{extractor_component}/{digest}"
        )
        destination = self._resolve_relative(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._assert_resolved_inside(destination.parent)
        reused = destination.exists()
        temp_path = destination.parent / (
            f".{destination.name}.{os.getpid()}-{secrets.token_hex(12)}.tmp"
        )
        try:
            if not reused:
                with temp_path.open("xb") as stream:
                    stream.write(output)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, destination)
                self._fsync_directory(destination.parent)
        except Exception as exc:
            _best_effort_unlink(temp_path)
            raise ArchiveWriteError("派生物临时写入或原子发布失败") from exc
        verified_digest, verified_length = _hash_file(destination)
        if verified_digest != digest or verified_length != len(output):
            self._quarantine_if_present(destination, "derived_integrity_mismatch")
            raise SnapshotIntegrityMismatch("派生物发布后完整哈希或长度不一致")
        return ArchivedDerivedArtifact(
            storage_namespace_id=self.storage_namespace_id,
            parent_snapshot_id=parent_snapshot_id,
            extractor_id=extractor_id,
            output_sha256=digest,
            byte_length=len(output),
            relative_path=relative_path,
            archived_at=datetime.fromtimestamp(
                destination.stat().st_mtime,
                tz=timezone.utc,
            ),
            reused=reused,
        )

    def read_verified_derived(
        self,
        relative_path: str,
        *,
        expected_sha256: str,
        expected_length: int | None = None,
    ) -> bytes:
        identifier = _validate_relative_identifier(relative_path)
        if not identifier.startswith("raw/derived/"):
            raise SnapshotPathEscape("派生物标识不属于 derived 目录")
        path = self._resolve_relative(identifier)
        try:
            output = path.read_bytes()
        except OSError as exc:
            raise SnapshotIntegrityMismatch("派生物缺失或不可读") from exc
        if expected_length is not None and len(output) != expected_length:
            raise SnapshotIntegrityMismatch("派生物长度不匹配")
        if hashlib.sha256(output).hexdigest() != _validate_sha256(expected_sha256):
            raise SnapshotIntegrityMismatch("派生物 SHA-256 不匹配")
        return output

    def _resolve_relative(self, relative_path: str) -> Path:
        identifier = _validate_relative_identifier(relative_path)
        candidate = (self.data_root / Path(*PurePosixPath(identifier).parts)).resolve()
        self._assert_resolved_inside(candidate)
        return candidate

    def _assert_resolved_inside(self, path: Path) -> None:
        try:
            path.relative_to(self.data_root)
        except ValueError as exc:
            raise SnapshotPathEscape("归档路径越出绑定 data_root") from exc

    def _quarantine_if_present(self, path: Path, reason: str) -> str | None:
        if not path.exists():
            return None
        self._assert_resolved_inside(path.resolve())
        quarantine = self._resolve_relative("quarantine/raw")
        quarantine.mkdir(parents=True, exist_ok=True)
        target = quarantine / f"{path.name}.{reason}.{secrets.token_hex(8)}"
        self._assert_resolved_inside(target.resolve())
        try:
            os.replace(path, target)
            self._fsync_directory(target.parent)
        except OSError:
            return None
        return target.relative_to(self.data_root).as_posix()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            descriptor = os.open(path, flags)
        except OSError:
            # Windows does not expose a portable directory fsync. File fsync and
            # os.replace above are still mandatory and already completed.
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class SnapshotCommitCoordinator:
    """Keep filesystem publication before the short SQLite metadata transaction."""

    def __init__(
        self,
        blob_store: ContentAddressedBlobStore,
        repository: SnapshotRepository,
    ) -> None:
        self.blob_store = blob_store
        self.repository = repository

    def publish_content_snapshot(
        self,
        content: bytes,
        build_bundle: Callable[[ArchivedBlob], BundleT],
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
        expected_sha256: str | None = None,
        expected_length: int | None = None,
    ) -> BundleT:
        archived = self.blob_store.archive_bytes(
            content,
            expected_sha256=expected_sha256,
            expected_length=expected_length,
        )
        bundle = build_bundle(archived)
        if not isinstance(bundle, tuple) or len(bundle) != 3:
            raise TypeError("build_bundle 必须返回 (ContentBlob, RawResourceSnapshot, ResourceObservation)")
        try:
            self.repository.commit_snapshot_bundle(
                *bundle,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
        except Exception as exc:
            raise SnapshotCommitError(
                "snapshot/blob/creating-observation 元数据事务提交失败",
                orphan_relative_path=archived.relative_path,
            ) from exc
        return bundle

    def publish_derived_artifact(
        self,
        *,
        parent_snapshot_id: str,
        extractor_id: str,
        output: bytes,
        build_artifact: Callable[[ArchivedDerivedArtifact], Any],
    ) -> Any:
        archived = self.blob_store.archive_derived_bytes(
            parent_snapshot_id=parent_snapshot_id,
            extractor_id=extractor_id,
            output=output,
        )
        artifact = build_artifact(archived)
        try:
            self.repository.save_derived_artifact(artifact)
        except Exception as exc:
            raise SnapshotCommitError(
                "DerivedArtifact 元数据提交失败",
                orphan_relative_path=archived.relative_path,
            ) from exc
        return artifact

    def publish_discovery_response_snapshot(
        self,
        content: bytes,
        build_bundle: Callable[[ArchivedBlob], BundleT],
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
        expected_sha256: str | None = None,
        expected_length: int | None = None,
    ) -> BundleT:
        archived = self.blob_store.archive_bytes(
            content,
            expected_sha256=expected_sha256,
            expected_length=expected_length,
        )
        bundle = build_bundle(archived)
        if not isinstance(bundle, tuple) or len(bundle) != 3:
            raise TypeError(
                "build_bundle 必须返回 (ContentBlob, RawResourceSnapshot, DiscoveryObservation)"
            )
        commit = getattr(
            self.repository,
            "commit_discovery_snapshot_bundle",
            getattr(self.repository, "commit_retained_discovery_snapshot", None),
        )
        if commit is None:
            raise SnapshotCommitError(
                "repository 不支持 discovery snapshot 原子提交",
                orphan_relative_path=archived.relative_path,
            )
        try:
            _call_repository(
                commit,
                *bundle,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
        except Exception as exc:
            raise SnapshotCommitError(
                "discovery snapshot/creating-observation 元数据事务提交失败",
                orphan_relative_path=archived.relative_path,
            ) from exc
        return bundle


class SnapshotService:
    """Create role-correct snapshots and append a new observation for every access."""

    def __init__(
        self,
        blob_store: ContentAddressedBlobStore,
        repository: SnapshotRepository,
    ) -> None:
        self.blob_store = blob_store
        self.repository = repository
        self.coordinator = SnapshotCommitCoordinator(blob_store, repository)

    def freeze_content(
        self,
        content: bytes,
        request: ContentSnapshotRequest,
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> FrozenSnapshotResult:
        archived = self.blob_store.archive_bytes(content)
        same = self._find_snapshot(
            source_definition_id=request.source_definition_id,
            source_definition_version=request.source_definition_version,
            resource_role=ResourceRole.CONTENT,
            canonical_resource_id=request.canonical_resource_id,
            sha256=archived.sha256,
        )
        latest = same or self._find_snapshot(
            source_definition_id=request.source_definition_id,
            source_definition_version=request.source_definition_version,
            resource_role=ResourceRole.CONTENT,
            canonical_resource_id=request.canonical_resource_id,
        )
        compatible_anchor = self._compatible_content_anchor(request)
        if same is None and compatible_anchor is not None and compatible_anchor.sha256 == archived.sha256:
            same = compatible_anchor
        blob = _content_blob(archived)
        if same is not None:
            observation = self._content_observation(
                request,
                snapshot=same,
                disposition=ResourceDisposition.UNCHANGED,
                outcome=AcquisitionOutcome.UNCHANGED,
            )
            _call_repository(
                self.repository.save_resource_observation,
                observation,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
            return FrozenSnapshotResult(
                blob=blob,
                snapshot=same,
                observation=observation,
                disposition=ResourceDisposition.UNCHANGED.value,
                created_snapshot=False,
            )

        pit = decide_available_at(
            published_at=request.published_at,
            published_at_precision=request.published_at_precision,
            source_timezone=request.source_timezone,
            retrieved_at=request.retrieved_at,
            # A new hash for a known canonical URL disproves the old byte
            # version's publication-time claim for these replacement bytes.
            immutable_version_proven=request.immutable_version_proven
            and latest is None and compatible_anchor is None,
        )
        version = 1 if latest is None else int(latest.version) + 1
        observation_id = request.observation_id or acquisition_new_id()
        identity = {
            "namespace": self.blob_store.storage_namespace_id,
            "source_definition_id": request.source_definition_id,
            "source_definition_version": request.source_definition_version,
            "role": ResourceRole.CONTENT.value,
            "canonical_resource_id": request.canonical_resource_id,
            "sha256": archived.sha256,
        }
        snapshot = RawResourceSnapshot(
            snapshot_id=stable_acquisition_id("snapshot", identity),
            resource_role=ResourceRole.CONTENT,
            storage_namespace_id=self.blob_store.storage_namespace_id,
            source_definition_id=request.source_definition_id,
            source_definition_version=request.source_definition_version,
            creating_observation_id=observation_id,
            content_blob_id=blob.content_blob_id,
            mime_type=request.mime_type,
            byte_length=archived.byte_length,
            sha256=archived.sha256,
            archive_relative_path=archived.relative_path,
            available_at=pit.available_at,
            available_at_basis=AvailableAtBasis(pit.available_at_basis),
            version=version,
            supersedes_snapshot_id=(None if latest is None else latest.snapshot_id),
            policy_decision=request.policy_decision,
            created_at=archived.archived_at,
            canonical_resource_id=request.canonical_resource_id,
            upstream_material_id=request.upstream_material_id,
            canonical_url=request.canonical_url,
            published_at_raw=request.published_at_raw,
            published_at=pit.published_at,
            published_at_precision=PublishedAtPrecision(pit.published_at_precision),
            source_timezone=request.source_timezone,
        )
        disposition = (
            ResourceDisposition.NEW if latest is None and compatible_anchor is None else ResourceDisposition.CHANGED
        )
        observation = self._content_observation(
            request,
            snapshot=snapshot,
            disposition=disposition,
            outcome=AcquisitionOutcome.SUCCESS,
            observation_id=observation_id,
        )
        try:
            _call_repository(
                self.repository.commit_snapshot_bundle,
                blob,
                snapshot,
                observation,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
        except Exception as exc:
            raise SnapshotCommitError(
                "snapshot/blob/creating-observation 元数据事务提交失败",
                orphan_relative_path=archived.relative_path,
            ) from exc
        return FrozenSnapshotResult(
            blob=blob,
            snapshot=snapshot,
            observation=observation,
            disposition=disposition.value,
            created_snapshot=True,
        )

    def _compatible_content_anchor(self, request: ContentSnapshotRequest) -> RawResourceSnapshot | None:
        if not request.validator_source_snapshot_id:
            return None
        from .validators import content_contract_compatible, snapshot_matches_resource
        anchor = self.repository.get_raw_resource_snapshot(request.validator_source_snapshot_id)
        if anchor.source_definition_version == request.source_definition_version:
            return None
        current = self.repository.get_source_definition_version(request.source_definition_id, request.source_definition_version)
        previous = self.repository.get_source_definition_version(anchor.source_definition_id, anchor.source_definition_version)
        resource = self.repository.get_discovered_resource(request.discovered_resource_id)
        if (not content_contract_compatible(current, previous)
                or not snapshot_matches_resource(anchor, resource, self.blob_store.storage_namespace_id)
                or request.final_url != anchor.canonical_url):
            return None
        events = self.repository.list_snapshot_integrity_events(anchor.snapshot_id)
        if events and max(events, key=lambda e: (e.checked_at, e.integrity_event_id)).status.value == "quarantined":
            return None
        try:
            self.blob_store.read_verified(anchor.archive_relative_path,
                expected_sha256=anchor.sha256, expected_length=anchor.byte_length)
        except SnapshotPipelineError:
            return None
        return anchor

    def freeze_discovery_response(
        self,
        content: bytes,
        request: DiscoverySnapshotRequest,
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> FrozenSnapshotResult:
        if request.page_number is None and request.cursor is None:
            raise ValueError("discovery response 必须固定 page_number 或 cursor")
        archived = self.blob_store.archive_bytes(content)
        same = self._find_snapshot(
            source_definition_id=request.source_definition_id,
            source_definition_version=request.source_definition_version,
            resource_role=ResourceRole.DISCOVERY_RESPONSE,
            query_page_canonical=request.query_page_canonical,
            sha256=archived.sha256,
        )
        latest = same or self._find_snapshot(
            source_definition_id=request.source_definition_id,
            source_definition_version=request.source_definition_version,
            resource_role=ResourceRole.DISCOVERY_RESPONSE,
            query_page_canonical=request.query_page_canonical,
        )
        blob = _content_blob(archived)
        observation_id = request.observation_id or acquisition_new_id()
        if same is not None:
            observation = self._discovery_observation(
                request,
                archived,
                snapshot_id=same.snapshot_id,
                observation_id=observation_id,
            )
            save = getattr(self.repository, "save_discovery_observation", None)
            if save is None:
                raise SnapshotCommitError("repository 不支持追加 discovery observation")
            _call_repository(
                save,
                observation,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
            return FrozenSnapshotResult(
                blob=blob,
                snapshot=same,
                observation=observation,
                disposition="unchanged",
                created_snapshot=False,
            )
        version = 1 if latest is None else int(latest.version) + 1
        identity = {
            "namespace": self.blob_store.storage_namespace_id,
            "source_definition_id": request.source_definition_id,
            "source_definition_version": request.source_definition_version,
            "role": ResourceRole.DISCOVERY_RESPONSE.value,
            "query_page_canonical": request.query_page_canonical,
            "sha256": archived.sha256,
        }
        snapshot = RawResourceSnapshot(
            snapshot_id=stable_acquisition_id("snapshot", identity),
            resource_role=ResourceRole.DISCOVERY_RESPONSE,
            storage_namespace_id=self.blob_store.storage_namespace_id,
            source_definition_id=request.source_definition_id,
            source_definition_version=request.source_definition_version,
            creating_observation_id=observation_id,
            content_blob_id=blob.content_blob_id,
            mime_type=request.mime_type,
            byte_length=archived.byte_length,
            sha256=archived.sha256,
            archive_relative_path=archived.relative_path,
            available_at=_aware_utc(request.retrieved_at),
            available_at_basis=AvailableAtBasis.RETRIEVED_AT,
            version=version,
            supersedes_snapshot_id=(None if latest is None else latest.snapshot_id),
            policy_decision=request.policy_decision,
            created_at=archived.archived_at,
            physical_query_plan_item_id=request.physical_query_plan_item_id,
            page_number=request.page_number,
            cursor=request.cursor,
            query_page_canonical=request.query_page_canonical,
        )
        observation = self._discovery_observation(
            request,
            archived,
            snapshot_id=snapshot.snapshot_id,
            observation_id=observation_id,
        )
        commit = getattr(
            self.repository,
            "commit_discovery_snapshot_bundle",
            getattr(self.repository, "commit_retained_discovery_snapshot", None),
        )
        if commit is None:
            raise SnapshotCommitError(
                "repository 不支持 discovery snapshot 原子提交",
                orphan_relative_path=archived.relative_path,
            )
        try:
            _call_repository(
                commit,
                blob,
                snapshot,
                observation,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
        except Exception as exc:
            raise SnapshotCommitError(
                "discovery snapshot/creating-observation 元数据事务提交失败",
                orphan_relative_path=archived.relative_path,
            ) from exc
        return FrozenSnapshotResult(
            blob=blob,
            snapshot=snapshot,
            observation=observation,
            disposition=("new" if latest is None else "changed"),
            created_snapshot=True,
        )

    def freeze_derived_artifact(
        self,
        *,
        parent_snapshot_id: str,
        artifact_type: str,
        extractor_id: str,
        extractor_version: str,
        parameters: Mapping[str, Any],
        output: bytes,
    ) -> DerivedArtifact:
        def build(archived: ArchivedDerivedArtifact) -> DerivedArtifact:
            identity = {
                "storage_namespace_id": archived.storage_namespace_id,
                "parent_snapshot_id": parent_snapshot_id,
                "artifact_type": artifact_type,
                "extractor_id": extractor_id,
                "extractor_version": extractor_version,
                "parameters": dict(parameters),
                "output_sha256": archived.output_sha256,
            }
            return DerivedArtifact(
                derived_artifact_id=stable_acquisition_id("derived", identity),
                storage_namespace_id=archived.storage_namespace_id,
                parent_snapshot_id=parent_snapshot_id,
                artifact_type=artifact_type,
                extractor_id=extractor_id,
                extractor_version=extractor_version,
                parameters=dict(parameters),
                output_sha256=archived.output_sha256,
                output_byte_length=archived.byte_length,
                archive_relative_path=archived.relative_path,
                created_at=archived.archived_at,
            )

        return self.coordinator.publish_derived_artifact(
            parent_snapshot_id=parent_snapshot_id,
            extractor_id=extractor_id,
            output=output,
            build_artifact=build,
        )

    def _find_snapshot(self, **filters: Any) -> RawResourceSnapshot | None:
        finder = getattr(self.repository, "find_raw_resource_snapshot", None)
        if finder is None:
            return None
        result = finder(**filters)
        if result is None:
            return None
        if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
            if not result:
                return None
            return max(result, key=lambda item: (int(item.version), item.snapshot_id))
        return result

    @staticmethod
    def _content_observation(
        request: ContentSnapshotRequest,
        *,
        snapshot: RawResourceSnapshot,
        disposition: ResourceDisposition,
        outcome: AcquisitionOutcome,
        observation_id: str | None = None,
    ) -> ResourceObservation:
        original_url = request.original_url or request.canonical_url
        final_url = request.final_url or request.canonical_url
        return ResourceObservation(
            observation_id=observation_id or request.observation_id or acquisition_new_id(),
            attempt_id=request.attempt_id,
            discovered_resource_id=request.discovered_resource_id,
            parent_discovery_attempt_id=request.parent_discovery_attempt_id,
            source_definition_id=request.source_definition_id,
            source_definition_version=request.source_definition_version,
            snapshot_id=snapshot.snapshot_id,
            disposition=disposition,
            attempt_outcome=outcome,
            original_url=_redacted_https_url(original_url),
            final_url=_redacted_https_url(final_url),
            redirect_chain=tuple(
                {"url": value} for value in redact_redirect_chain(request.redirect_chain)
            ),
            request_summary=sanitize_http_metadata(request.request_summary or {}),
            response_summary=sanitize_http_metadata(request.response_summary or {}),
            http_status=request.http_status,
            etag=request.etag,
            last_modified=request.last_modified,
            validator_source_snapshot_id=request.validator_source_snapshot_id,
            observed_at=_aware_utc(request.observed_at),
            retrieved_at=_aware_utc(request.retrieved_at),
            reason_code=request.reason_code,
        )

    @staticmethod
    def _discovery_observation(
        request: DiscoverySnapshotRequest,
        archived: ArchivedBlob,
        *,
        snapshot_id: str,
        observation_id: str,
    ) -> DiscoveryObservation:
        return DiscoveryObservation(
            observation_id=observation_id,
            attempt_id=request.attempt_id,
            physical_query_plan_item_id=request.physical_query_plan_item_id,
            source_definition_id=request.source_definition_id,
            source_definition_version=request.source_definition_version,
            page_number=request.page_number,
            cursor=request.cursor,
            observed_at=_aware_utc(request.observed_at),
            retrieved_at=_aware_utc(request.retrieved_at),
            http_status=request.http_status,
            mime_type=request.mime_type,
            response_sha256=archived.sha256,
            response_byte_length=archived.byte_length,
            snapshot_id=snapshot_id,
            request_summary=sanitize_http_metadata(request.request_summary or {}),
            response_summary=sanitize_http_metadata(request.response_summary or {}),
        )

class SnapshotIntegrityService:
    """Append integrity facts without modifying immutable snapshot records."""

    def __init__(
        self,
        blob_store: ContentAddressedBlobStore,
        repository: SnapshotRepository,
        *,
        reconcile_candidate_sink: Callable[[Any, SnapshotIntegrityEvent], Any] | None = None,
    ) -> None:
        self.blob_store = blob_store
        self.repository = repository
        self.reconcile_candidate_sink = reconcile_candidate_sink

    def verify_snapshot(
        self,
        snapshot_id: str,
        *,
        checked_at: datetime | None = None,
    ) -> IntegrityCheckResult:
        snapshot = self.repository.get_raw_resource_snapshot(snapshot_id)
        observed_sha256: str | None = None
        observed_length: int | None = None
        reason: str | None = self._lineage_issue(snapshot)
        try:
            path = self.blob_store.resolve_blob(snapshot.archive_relative_path)
            observed_sha256, observed_length = _hash_file(path)
        except (OSError, SnapshotPathEscape):
            reason = reason or "blob_missing"
        if reason is None and observed_length != int(snapshot.byte_length):
            reason = "blob_length_mismatch"
        if reason is None and observed_sha256 != str(snapshot.sha256):
            reason = "blob_hash_mismatch"
        event = SnapshotIntegrityEvent(
            snapshot_id=snapshot_id,
            status=(
                SnapshotIntegrityStatus.VERIFIED
                if reason is None
                else SnapshotIntegrityStatus.QUARANTINED
            ),
            checked_at=_aware_utc(checked_at or datetime.now(timezone.utc)),
            observed_sha256=observed_sha256,
            observed_byte_length=observed_length,
            reason_code=reason,
        )
        self.repository.append_snapshot_integrity_event(event)
        candidate = None
        if reason is not None and self.reconcile_candidate_sink is not None:
            candidate = self.reconcile_candidate_sink(snapshot, event)
        return IntegrityCheckResult(event=event, reconcile_candidate=candidate)

    def current_status(self, snapshot_id: str) -> SnapshotIntegrityStatus | None:
        events = self.repository.list_snapshot_integrity_events(snapshot_id)
        if not events:
            return None
        latest = max(
            events,
            key=lambda item: (item.checked_at, item.integrity_event_id),
        )
        return SnapshotIntegrityStatus(latest.status)

    def _lineage_issue(self, snapshot: RawResourceSnapshot) -> str | None:
        if snapshot.storage_namespace_id != self.blob_store.storage_namespace_id:
            return "namespace_mismatch"
        get_blob = getattr(self.repository, "get_content_blob", None)
        if get_blob is not None:
            try:
                blob = get_blob(snapshot.content_blob_id)
            except Exception:
                return "content_blob_reference_missing"
            if (
                blob.storage_namespace_id != snapshot.storage_namespace_id
                or blob.sha256 != snapshot.sha256
                or int(blob.byte_length) != int(snapshot.byte_length)
                or blob.archive_relative_path != snapshot.archive_relative_path
            ):
                return "content_blob_reference_mismatch"
        if snapshot.supersedes_snapshot_id:
            try:
                parent = self.repository.get_raw_resource_snapshot(
                    snapshot.supersedes_snapshot_id
                )
            except Exception:
                return "supersedes_reference_missing"
            same_identity = (
                parent.source_definition_id == snapshot.source_definition_id
                and parent.source_definition_version
                == snapshot.source_definition_version
                and parent.resource_role == snapshot.resource_role
                and int(parent.version) + 1 == int(snapshot.version)
            )
            if snapshot.resource_role == ResourceRole.CONTENT:
                same_identity = same_identity and (
                    parent.canonical_resource_id == snapshot.canonical_resource_id
                )
            else:
                same_identity = same_identity and (
                    parent.query_page_canonical == snapshot.query_page_canonical
                )
            if not same_identity:
                return "supersedes_reference_mismatch"
        return None


def decide_available_at(
    *,
    published_at: datetime | date | None,
    published_at_precision: str,
    source_timezone: str,
    retrieved_at: datetime,
    immutable_version_proven: bool,
) -> PointInTimeDecision:
    """Calculate the earliest safe public time without inventing precision."""

    retrieved = _aware_utc(retrieved_at)
    try:
        source_zone = ZoneInfo(source_timezone)
    except Exception as exc:
        raise ValueError(f"未知来源时区: {source_timezone}") from exc
    precision = str(getattr(published_at_precision, "value", published_at_precision)).lower()
    normalized_published: datetime | None = None
    if isinstance(published_at, datetime):
        normalized_published = _aware_utc(published_at)

    if precision == "instant" and normalized_published is not None and immutable_version_proven:
        return PointInTimeDecision(
            available_at=normalized_published,
            available_at_basis="verified_published_instant",
            published_at=normalized_published,
            published_at_precision="instant",
            source_timezone=source_timezone,
        )
    if precision == "date" and published_at is not None:
        if isinstance(published_at, datetime):
            local_date = (
                published_at.replace(tzinfo=source_zone)
                if published_at.tzinfo is None
                else published_at.astimezone(source_zone)
            ).date()
        else:
            local_date = published_at
        next_boundary = datetime.combine(
            local_date + timedelta(days=1),
            time.min,
            tzinfo=source_zone,
        ).astimezone(timezone.utc)
        normalized_published = datetime.combine(
            local_date,
            time.min,
            tzinfo=source_zone,
        ).astimezone(timezone.utc)
        if immutable_version_proven:
            return PointInTimeDecision(
                available_at=next_boundary,
                available_at_basis="source_date_next_boundary",
                published_at=normalized_published,
                published_at_precision="date",
                source_timezone=source_timezone,
            )
    return PointInTimeDecision(
        available_at=retrieved,
        available_at_basis="retrieved_at",
        published_at=(
            None if precision == "unknown" else normalized_published
        ),
        published_at_precision=(precision if precision in {"instant", "date", "unknown"} else "unknown"),
        source_timezone=source_timezone,
    )


def is_point_in_time_eligible(available_at: datetime, as_of: datetime) -> bool:
    return _aware_utc(available_at) <= _aware_utc(as_of)


def canonical_json_sha256(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _namespace_id(storage_namespace: Any) -> str:
    if isinstance(storage_namespace, str):
        return storage_namespace.strip()
    for field in ("storage_namespace_id", "namespace_id"):
        value = getattr(storage_namespace, field, None)
        if value:
            return str(value).strip()
    if isinstance(storage_namespace, Mapping):
        for field in ("storage_namespace_id", "namespace_id"):
            if storage_namespace.get(field):
                return str(storage_namespace[field]).strip()
    return ""


def _content_blob(archived: ArchivedBlob) -> ContentBlob:
    return ContentBlob(
        content_blob_id=f"blob-sha256-{archived.sha256}",
        storage_namespace_id=archived.storage_namespace_id,
        sha256=archived.sha256,
        byte_length=archived.byte_length,
        archive_relative_path=archived.relative_path,
        created_at=archived.archived_at,
    )


def _call_repository(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Pass fencing kwargs when the repository contract supports them."""

    try:
        signature = inspect.signature(call)
    except (TypeError, ValueError):
        return call(*args, **kwargs)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    filtered = {
        key: value
        for key, value in kwargs.items()
        if value is not None and (accepts_kwargs or key in signature.parameters)
    }
    return call(*args, **filtered)


def _redacted_https_url(value: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("observation URL 必须是 HTTPS")
    # redact_url always removes credentials and sensitive values. The Pydantic
    # model then enforces the remaining HTTPS/no-fragment boundary.
    from .security import redact_url

    return redact_url(value)


def _validate_sha256(value: str) -> str:
    digest = value.strip().lower()
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("SHA-256 必须是完整 64 位十六进制")
    return digest


def _validate_relative_identifier(value: str) -> str:
    if not value or "\\" in value:
        raise SnapshotPathEscape("归档标识必须是非空 POSIX 相对路径")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SnapshotPathEscape("归档标识不得为绝对路径或包含路径穿越")
    if ":" in pure.parts[0]:
        raise SnapshotPathEscape("归档标识不得包含 Windows 盘符")
    return pure.as_posix()


def _safe_component(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise SnapshotPathEscape(f"{field_name} 不是安全路径组件")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
        raise SnapshotPathEscape(f"{field_name} 不是安全路径组件")
    if normalized in {".", ".."}:
        raise SnapshotPathEscape(f"{field_name} 不是安全路径组件")
    return normalized


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _aware_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"无法规范化 JSON 类型: {type(value).__name__}")
