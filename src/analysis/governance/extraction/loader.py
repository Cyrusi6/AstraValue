from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any

from ..models import SourceRole
from ..ports import GovernanceAcquisitionPort


_MISSING = object()
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


class ManifestBoundLoadError(ValueError):
    """Fail-closed error raised before untrusted artifact bytes are consumed."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ManifestBoundArtifact:
    """Governance-local, read-only view of a shared manifest artifact.

    This is deliberately not a replacement for the shared acquisition
    manifest/blob models.  It only carries the fields the extraction boundary
    must verify before handing content to an extractor.
    """

    manifest_id: str
    artifact_id: str
    artifact_kind: str
    raw_snapshot_id: str
    content_hash: str
    source_role: SourceRole
    upstream_material_id: str
    independence_group: str
    integrity_verified: bool
    lineage_complete: bool
    llm_allowed: bool
    announced_at: datetime | None
    available_at: datetime | None
    retrieved_at: datetime | None
    derived_artifact_id: str | None
    derived_artifact_hash: str | None
    content: object


def _value(subject: object, *names: str, default: object = _MISSING) -> object:
    for name in names:
        if isinstance(subject, Mapping) and name in subject:
            return subject[name]
        if hasattr(subject, name):
            return getattr(subject, name)
    if default is _MISSING:
        raise KeyError(names[0])
    return default


def _nonempty_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestBoundLoadError("lineage_incomplete", f"{field} is required")
    return value.strip()


def _optional_datetime(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ManifestBoundLoadError(
                "lineage_invalid", f"{field} is not an ISO datetime"
            ) from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ManifestBoundLoadError(
            "lineage_invalid", f"{field} must be timezone-aware"
        )
    return value


def _status_passed(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {
            "passed",
            "verified",
            "valid",
            "complete",
            "ok",
        }
    return False


def _policy_allowed(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {
            "allowed",
            "allow",
            "enabled",
            "permitted",
        }
    if isinstance(value, Mapping):
        nested = _value(
            value,
            "allowed",
            "allow",
            "enabled",
            "for_llm",
            default=False,
        )
        return _policy_allowed(nested)
    return False


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _content_from_verified_bytes(value: bytes) -> object:
    """Decode structured JSON only from bytes whose manifest hash was verified."""

    try:
        return json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return value


def _looks_like_external_location(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    if _URI.match(stripped):
        return True
    if stripped.startswith(("./", "../", ".\\", "..\\", "\\\\")):
        return True
    if "/" in stripped or "\\" in stripped:
        return True
    return PureWindowsPath(stripped).is_absolute() or PurePosixPath(stripped).is_absolute()


def _descriptor_id(value: object) -> str | None:
    if isinstance(value, str):
        return value
    candidate = _value(
        value,
        "artifact_id",
        "derived_artifact_id",
        "raw_snapshot_id",
        "id",
        default=None,
    )
    return candidate if isinstance(candidate, str) else None


def _manifest_descriptors(manifest: object) -> tuple[object, ...]:
    descriptors: list[object] = []
    for name in (
        "artifacts",
        "artifact_entries",
        "entries",
        "raw_snapshots",
        "derived_artifacts",
        "artifact_ids",
        "raw_snapshot_ids",
        "derived_artifact_ids",
    ):
        container = _value(manifest, name, default=None)
        if container is None:
            continue
        if isinstance(container, Mapping):
            for key, item in container.items():
                if isinstance(item, Mapping):
                    if _descriptor_id(item) is None:
                        descriptors.append({"artifact_id": str(key), **dict(item)})
                    else:
                        descriptors.append(item)
                elif isinstance(item, str):
                    descriptors.append(item)
                else:
                    descriptors.append({"artifact_id": str(key), "value": item})
            continue
        if isinstance(container, Sequence) and not isinstance(
            container, (str, bytes, bytearray)
        ):
            descriptors.extend(container)
    return tuple(descriptors)


def _bound_value(
    descriptor: object,
    loaded: object,
    manifest: object,
    *names: str,
    default: object = None,
) -> object:
    for source in (descriptor, loaded, manifest):
        try:
            value = _value(source, *names)
        except KeyError:
            continue
        if value is not None:
            return value
    return default


class ManifestBoundExtractionLoader:
    """Load only artifacts explicitly frozen into a shared evidence manifest."""

    def __init__(self, port: GovernanceAcquisitionPort[Any, Any, Any, Any, Any, Any]) -> None:
        self._port = port

    def load(
        self,
        manifest_id: str,
        artifact_id: str,
        *,
        for_llm: bool = False,
    ) -> ManifestBoundArtifact:
        if not isinstance(manifest_id, str) or not manifest_id.strip():
            raise ManifestBoundLoadError("manifest_id_invalid", "manifest_id is required")
        if not isinstance(artifact_id, str) or _looks_like_external_location(artifact_id):
            raise ManifestBoundLoadError(
                "arbitrary_location_rejected",
                "artifact_id must be a manifest member ID, not a path or URL",
            )

        manifest = self._port.get_manifest(manifest_id)
        actual_manifest_id = _value(
            manifest, "manifest_id", "evidence_manifest_id", "id", default=None
        )
        if actual_manifest_id != manifest_id:
            raise ManifestBoundLoadError(
                "manifest_identity_mismatch", "port returned a different manifest"
            )

        descriptor = next(
            (
                item
                for item in _manifest_descriptors(manifest)
                if _descriptor_id(item) == artifact_id
            ),
            None,
        )
        if descriptor is None:
            raise ManifestBoundLoadError(
                "artifact_not_in_manifest", "artifact is not explicitly listed by manifest"
            )

        # All authorization checks happen before load_artifact, so a rejected ID
        # cannot be used as an arbitrary read primitive through the port.
        integrity_marker = _bound_value(
            descriptor,
            {},
            manifest,
            "integrity_verified",
            "integrity_passed",
            "raw_hash_verified",
            "integrity_status",
            "verification_status",
            default=False,
        )
        if not _status_passed(integrity_marker):
            raise ManifestBoundLoadError(
                "integrity_not_verified", "manifest has not verified artifact integrity"
            )

        llm_marker = _bound_value(
            descriptor,
            {},
            manifest,
            "llm_allowed",
            "allow_llm",
            "llm_policy",
            default=False,
        )
        llm_allowed = _policy_allowed(llm_marker)
        if for_llm and not llm_allowed:
            raise ManifestBoundLoadError(
                "llm_policy_denied", "manifest/source policy does not allow LLM access"
            )

        # Lineage must be asserted by the manifest itself.  Metadata returned
        # with content cannot self-authorize content that the manifest did not
        # bind before the read.
        lineage_marker = _bound_value(
            descriptor,
            {},
            manifest,
            "lineage_complete",
            default=True,
        )
        if lineage_marker is not True:
            raise ManifestBoundLoadError(
                "lineage_incomplete", "manifest marks artifact lineage incomplete"
            )
        raw_snapshot_id = _nonempty_text(
            _bound_value(
                descriptor,
                {},
                manifest,
                "raw_snapshot_id",
                default=(artifact_id if "raw" in artifact_id.lower() else None),
            ),
            field="raw_snapshot_id",
        )
        content_hash = _nonempty_text(
            _bound_value(
                descriptor,
                {},
                manifest,
                "content_hash",
                "sha256",
                "blob_hash",
            ),
            field="content_hash",
        )
        if not _SHA256.fullmatch(content_hash):
            raise ManifestBoundLoadError(
                "content_hash_invalid", "content_hash must be lowercase SHA-256"
            )
        upstream_material_id = _nonempty_text(
            _bound_value(
                descriptor,
                {},
                manifest,
                "upstream_material_id",
                "material_id",
            ),
            field="upstream_material_id",
        )
        independence_group = _nonempty_text(
            _bound_value(
                descriptor,
                {},
                manifest,
                "independence_group",
                "source_independence_group",
            ),
            field="independence_group",
        )
        source_role_value = _bound_value(
            descriptor, {}, manifest, "source_role", default=None
        )
        try:
            source_role = (
                source_role_value
                if isinstance(source_role_value, SourceRole)
                else SourceRole(str(source_role_value))
            )
        except (TypeError, ValueError) as exc:
            raise ManifestBoundLoadError(
                "lineage_invalid", "source_role is missing or unsupported"
            ) from exc
        kind = str(
            _bound_value(
                descriptor,
                {},
                manifest,
                "artifact_kind",
                "kind",
                default="raw_snapshot",
            )
        )
        derived_artifact_id_value = _bound_value(
            descriptor, {}, manifest, "derived_artifact_id", default=None
        )
        derived_artifact_id = (
            str(derived_artifact_id_value)
            if derived_artifact_id_value is not None
            else (artifact_id if "derived" in kind.lower() else None)
        )
        derived_hash_value = _bound_value(
            descriptor,
            {},
            manifest,
            "derived_artifact_hash",
            default=None,
        )
        derived_artifact_hash = (
            str(derived_hash_value) if derived_hash_value is not None else None
        )
        if (derived_artifact_id is None) != (derived_artifact_hash is None):
            raise ManifestBoundLoadError(
                "derived_lineage_incomplete",
                "derived artifact ID and hash must be supplied together",
            )
        if derived_artifact_hash is not None and not _SHA256.fullmatch(
            derived_artifact_hash
        ):
            raise ManifestBoundLoadError(
                "derived_hash_invalid", "derived artifact hash must be SHA-256"
            )

        # Membership, integrity, policy and manifest lineage have passed; only
        # now may bytes/content be read.
        loaded = self._port.load_artifact(manifest_id, artifact_id, for_llm=for_llm)

        loaded_id = _value(
            loaded, "artifact_id", "derived_artifact_id", "id", default=None
        )
        if loaded_id is not None and loaded_id != artifact_id:
            raise ManifestBoundLoadError(
                "artifact_identity_mismatch", "loaded artifact ID differs from manifest member"
            )

        loaded_raw_id = _value(loaded, "raw_snapshot_id", default=None)
        if loaded_raw_id is not None and loaded_raw_id != raw_snapshot_id:
            raise ManifestBoundLoadError(
                "raw_snapshot_mismatch", "loaded artifact is bound to another raw snapshot"
            )
        loaded_hash = _value(loaded, "content_hash", "sha256", "blob_hash", default=None)
        if loaded_hash is not None and loaded_hash != content_hash:
            raise ManifestBoundLoadError(
                "content_hash_mismatch", "loaded artifact hash differs from manifest"
            )
        byte_content = (
            loaded
            if isinstance(loaded, (bytes, bytearray, memoryview))
            else _value(loaded, "content_bytes", "raw_bytes", default=None)
        )
        if not isinstance(byte_content, (bytes, bytearray, memoryview)):
            raise ManifestBoundLoadError(
                "content_bytes_missing",
                "loaded artifact must provide authoritative content bytes",
            )
        authoritative_bytes = bytes(byte_content)
        expected_bytes_hash = derived_artifact_hash or content_hash
        actual_hash = hashlib.sha256(authoritative_bytes).hexdigest()
        if actual_hash != expected_bytes_hash:
            raise ManifestBoundLoadError(
                "content_hash_mismatch", "loaded bytes fail the manifest SHA-256"
            )

        # Parsed sidecars are not authoritative: downstream extraction consumes
        # only a value decoded from the bytes verified above.
        content = _content_from_verified_bytes(authoritative_bytes)
        return ManifestBoundArtifact(
            manifest_id=manifest_id,
            artifact_id=artifact_id,
            artifact_kind=kind,
            raw_snapshot_id=raw_snapshot_id,
            content_hash=content_hash,
            source_role=source_role,
            upstream_material_id=upstream_material_id,
            independence_group=independence_group,
            integrity_verified=True,
            lineage_complete=True,
            llm_allowed=llm_allowed,
            announced_at=_optional_datetime(
                _bound_value(
                    descriptor, loaded, manifest, "announced_at", default=None
                ),
                field="announced_at",
            ),
            available_at=_optional_datetime(
                _bound_value(
                    descriptor, loaded, manifest, "available_at", default=None
                ),
                field="available_at",
            ),
            retrieved_at=_optional_datetime(
                _bound_value(
                    descriptor, loaded, manifest, "retrieved_at", default=None
                ),
                field="retrieved_at",
            ),
            derived_artifact_id=derived_artifact_id,
            derived_artifact_hash=derived_artifact_hash,
            content=_freeze(content),
        )


__all__ = [
    "ManifestBoundArtifact",
    "ManifestBoundExtractionLoader",
    "ManifestBoundLoadError",
]
