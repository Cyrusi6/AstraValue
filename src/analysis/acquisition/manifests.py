from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .content_selection import (
    CONTENT_EXCLUSION_REASONS,
    content_exclusion_reason,
)
from .models import (
    EvidenceManifestExclusion,
    EvidenceManifestItem,
    EvidenceSnapshotManifest,
    LiveAccessReviewStatus,
    PolicyDecision,
    ResourceRole,
    SourcePolicyStatus,
    SourceDefinition,
    SourceRegistry,
    canonical_json_sha256 as model_sha256,
    stable_acquisition_id,
)
from .snapshots import (
    ContentAddressedBlobStore,
    SnapshotIntegrityMismatch,
    SnapshotPathEscape,
    canonical_json_sha256,
    is_point_in_time_eligible,
)


class EvidenceGateError(RuntimeError):
    def __init__(self, reason_code: str, message: str, *, exclusions: Sequence[EvidenceManifestExclusion] = ()) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.exclusions = tuple(exclusions)


class ManifestRepository(Protocol):
    def get_discovered_resource(self, discovered_resource_id: str) -> Any: ...
    def get_attempt(self, attempt_id: str) -> Any: ...
    def get_run(self, run_id: str) -> Any: ...
    def get_source_registry_version(self, registry_id: str, version: str) -> Any: ...
    def get_source_definition_version(self, source_id: str, version: str) -> Any: ...
    def get_raw_resource_snapshot(self, snapshot_id: str) -> Any: ...
    def list_snapshot_integrity_events(self, snapshot_id: str) -> Sequence[Any]: ...
    def get_derived_artifact(self, artifact_id: str) -> Any: ...
    def save_evidence_manifest(
        self, manifest: EvidenceSnapshotManifest, items: Sequence[Any] = ()
    ) -> Any: ...


class EvidenceManifestService:
    """Build the frozen gate future LLM code must validate before reading bytes."""

    _NON_BLOCKING_EXCLUSIONS = frozenset(
        {"discovery_response_audit_only", "proof_audit_only", *CONTENT_EXCLUSION_REASONS}
    )

    def __init__(
        self,
        blob_store: ContentAddressedBlobStore,
        repository: ManifestRepository,
        source_policy_resolver: Callable[[str, str], Any],
    ) -> None:
        self.blob_store = blob_store
        self.repository = repository
        self.source_policy_resolver = source_policy_resolver

    def build_evidence_manifest(
        self,
        *,
        run_id: str,
        question_set_version: str,
        registry_id: str,
        registry_version: str,
        as_of: datetime,
        snapshot_ids: Iterable[str],
        derived_artifact_ids: Iterable[str] = (),
        audit_reference_snapshot_ids: Iterable[str] = (),
        audit_proof_ids: Iterable[str] = (),
        resource_exclusions: Iterable[EvidenceManifestExclusion] = (),
        coverage_summary: Mapping[str, Any] | None = None,
        fail_on_ineligible: bool = True,
        default_consume_eligible: bool = True,
    ) -> EvidenceSnapshotManifest:
        cutoff = _aware_utc(as_of)
        artifacts_by_snapshot = self._load_artifacts(derived_artifact_ids)
        items: list[EvidenceManifestItem] = []
        exclusions: list[EvidenceManifestExclusion] = []
        policy_decisions: set[str] = set()
        audit_snapshot_ids = set(audit_reference_snapshot_ids)
        selected_exclusions = self._validate_resource_exclusions(resource_exclusions, run_id)

        for snapshot_id in sorted(set(snapshot_ids)):
            try:
                snapshot = self.repository.get_raw_resource_snapshot(snapshot_id)
            except Exception:
                exclusions.append(_exclusion("snapshot", snapshot_id, "snapshot_missing"))
                continue
            if _enum_value(_get(snapshot, "resource_role")) != ResourceRole.CONTENT.value:
                audit_snapshot_ids.add(snapshot_id)
                exclusions.append(
                    _exclusion("snapshot", snapshot_id, "discovery_response_audit_only")
                )
                continue
            issue, policy_decision = self._validate_content_snapshot(snapshot, cutoff)
            if policy_decision:
                policy_decisions.add(policy_decision)
            if issue:
                exclusions.append(_exclusion("snapshot", snapshot_id, issue))
                continue
            artifacts = tuple(
                sorted(artifacts_by_snapshot.get(snapshot_id, ()), key=_artifact_id)
            )
            artifact_issue = self._validate_artifacts(snapshot_id, artifacts)
            if artifact_issue:
                exclusions.append(_exclusion("snapshot", snapshot_id, artifact_issue))
                continue
            items.append(
                EvidenceManifestItem(
                    snapshot_id=snapshot_id,
                    derived_artifact_ids=tuple(_artifact_id(value) for value in artifacts),
                    derived_output_hashes=tuple(
                        _artifact_sha256(value) for value in artifacts
                    ),
                    source_definition_id=str(_get(snapshot, "source_definition_id")),
                    source_definition_version=str(
                        _get(snapshot, "source_definition_version")
                    ),
                    snapshot_sha256=_snapshot_sha256(snapshot),
                    available_at=_get(snapshot, "available_at"),
                )
            )

        for proof_id in sorted(set(audit_proof_ids)):
            exclusions.append(_exclusion("proof", proof_id, "proof_audit_only"))
        exclusions.extend(selected_exclusions)
        self._validate_audit_snapshot_references(tuple(sorted(audit_snapshot_ids)))
        blocking = tuple(
            item
            for item in exclusions
            if item.reason_code not in self._NON_BLOCKING_EXCLUSIONS
        )
        if blocking and fail_on_ineligible:
            raise EvidenceGateError(
                "evidence_gate_failed",
                "证据清单含缺失、篡改、未来可得、隔离或 LLM 禁止的内容",
                exclusions=blocking,
            )

        gate_passed = bool(items) and not blocking and default_consume_eligible
        payload = _manifest_payload(
            storage_namespace_id=self.blob_store.storage_namespace_id,
            run_id=run_id,
            question_set_version=question_set_version,
            registry_id=registry_id,
            registry_version=registry_version,
            as_of=cutoff,
            items=tuple(items),
            exclusions=tuple(exclusions),
            coverage_summary=coverage_summary or {},
            policy_decisions=tuple(sorted(policy_decisions)),
            gate_passed=gate_passed,
        )
        manifest_hash = canonical_json_sha256(payload)
        created_at = self._publish_manifest(manifest_hash, payload)
        manifest = EvidenceSnapshotManifest(
            manifest_id=stable_acquisition_id("manifest", payload),
            storage_namespace_id=self.blob_store.storage_namespace_id,
            run_id=run_id,
            question_set_version=question_set_version,
            registry_id=registry_id,
            registry_version=registry_version,
            as_of=cutoff,
            items=tuple(items),
            exclusions=tuple(exclusions),
            coverage_summary=dict(coverage_summary or {}),
            policy_decisions=tuple(sorted(policy_decisions)),
            manifest_hash=manifest_hash,
            gate_passed=gate_passed,
            created_at=created_at,
        )
        try:
            self.repository.save_evidence_manifest(
                manifest, items=(*manifest.items, *manifest.exclusions)
            )
        except Exception as exc:
            raise EvidenceGateError(
                "manifest_commit_failed", "EvidenceSnapshotManifest 提交失败"
            ) from exc
        return manifest

    def validate_evidence_manifest(
        self, manifest: EvidenceSnapshotManifest | Mapping[str, Any]
    ) -> EvidenceSnapshotManifest:
        record = (
            manifest
            if isinstance(manifest, EvidenceSnapshotManifest)
            else EvidenceSnapshotManifest.model_validate(manifest)
        )
        if record.storage_namespace_id != self.blob_store.storage_namespace_id:
            raise EvidenceGateError(
                "namespace_mismatch", "manifest 与 data_root namespace 不匹配"
            )
        payload = _manifest_payload(
            storage_namespace_id=record.storage_namespace_id,
            run_id=record.run_id,
            question_set_version=record.question_set_version,
            registry_id=record.registry_id,
            registry_version=record.registry_version,
            as_of=record.as_of,
            items=record.items,
            exclusions=record.exclusions,
            coverage_summary=record.coverage_summary,
            policy_decisions=record.policy_decisions,
            gate_passed=record.gate_passed,
        )
        if canonical_json_sha256(payload) != record.manifest_hash:
            raise EvidenceGateError(
                "manifest_hash_mismatch", "manifest canonical hash 不匹配"
            )
        if self._read_manifest_payload(record.manifest_hash) != payload:
            raise EvidenceGateError(
                "manifest_file_mismatch", "manifest 文件与冻结内容不一致"
            )
        self._validate_resource_exclusions(
            (item for item in record.exclusions if item.object_type == "resource"),
            record.run_id,
        )
        if not record.gate_passed:
            raise EvidenceGateError(
                "manifest_not_consume_eligible", "该 manifest 仅供部分审计"
            )
        for item in record.items:
            try:
                snapshot = self.repository.get_raw_resource_snapshot(item.snapshot_id)
            except Exception as exc:
                raise EvidenceGateError(
                    "snapshot_missing", "manifest 内容快照缺失"
                ) from exc
            issue, _ = self._validate_content_snapshot(snapshot, record.as_of)
            if issue:
                raise EvidenceGateError(issue, "manifest 内容快照不再通过门禁")
            if _snapshot_sha256(snapshot) != item.snapshot_sha256:
                raise EvidenceGateError(
                    "snapshot_hash_mismatch", "manifest 快照哈希引用漂移"
                )
            try:
                artifacts = tuple(
                    self.repository.get_derived_artifact(artifact_id)
                    for artifact_id in item.derived_artifact_ids
                )
            except Exception as exc:
                raise EvidenceGateError(
                    "derived_artifact_missing", "manifest 派生物缺失"
                ) from exc
            issue = self._validate_artifacts(item.snapshot_id, artifacts)
            if issue:
                raise EvidenceGateError(issue, "manifest 派生物不再通过门禁")
            if tuple(_artifact_sha256(value) for value in artifacts) != item.derived_output_hashes:
                raise EvidenceGateError(
                    "derived_hash_mismatch", "manifest 派生物哈希引用漂移"
                )
        return record

    def _validate_resource_exclusions(
        self, values: Iterable[EvidenceManifestExclusion], run_id: str
    ) -> tuple[EvidenceManifestExclusion, ...]:
        """Accept only the explicit, persisted body-selection decision in this run."""
        validated: dict[str, EvidenceManifestExclusion] = {}
        definitions = None
        for item in values:
            try:
                if item.object_type != "resource" or item.reason_code not in CONTENT_EXCLUSION_REASONS:
                    raise ValueError("unsupported_resource_exclusion")
                if definitions is None:
                    definitions = self._selection_definitions_for_run(run_id)
                resource = self.repository.get_discovered_resource(item.object_id)
                attempt = self.repository.get_attempt(_get(resource, "discovery_attempt_id"))
                source_id = _get(resource, "source_definition_id")
                version = _get(resource, "source_definition_version")
                policy = definitions[(source_id, version)]
                decision = (_get(resource, "metadata") or {}).get("content_selection", {})
                if (
                    _get(attempt, "run_id") != run_id
                    or _get(attempt, "source_definition_id") != source_id
                    or _get(attempt, "source_definition_version") != version
                    or _get(resource, "required_fetch") is not False
                    or decision.get("policy_id") != _get(policy, "content_selection_policy")
                    or decision.get("action") != "metadata_only"
                    or decision.get("reason_code") != item.reason_code
                    or decision.get("original_required_fetch") is not True
                    or content_exclusion_reason(
                        _get(resource, "title"), _get(resource, "resource_url"),
                        _get(resource, "expected_mime_types"), _get(policy, "content_selection_policy"),
                    ) != item.reason_code
                ):
                    raise ValueError("invalid_content_selection_lineage")
            except Exception as exc:
                raise EvidenceGateError(
                    "resource_exclusion_invalid", "正文排除缺少本运行的有效目录行与冻结策略"
                ) from exc
            validated[item.object_id] = item
        return tuple(validated[key] for key in sorted(validated))

    def _selection_definitions_for_run(self, run_id: str) -> dict[tuple[str, str], SourceDefinition]:
        run = self.repository.get_run(run_id)
        if run.storage_namespace_id != self.blob_store.storage_namespace_id:
            raise ValueError("resource_exclusion_namespace_mismatch")
        registry = SourceRegistry.model_validate(self.repository.get_source_registry_version(
            run.registry_id, run.registry_version,
        ))
        if (registry.registry_id != run.registry_id
                or str(registry.registry_version) != str(run.registry_version)
                or model_sha256(registry) != run.registry_content_hash):
            raise ValueError("resource_exclusion_registry_mismatch")
        stored = {(d.source_definition_id, str(d.version)): d
                  for d in (*registry.definitions, *registry.legacy_definitions)}
        result = {}
        for ref in run.source_definition_refs:
            key = (ref.source_definition_id, str(ref.version))
            definition = SourceDefinition.model_validate(
                self.repository.get_source_definition_version(*key)
            )
            if (model_sha256(definition) != ref.content_hash
                    or model_sha256(stored[key]) != ref.content_hash):
                raise ValueError("resource_exclusion_definition_mismatch")
            result[key] = definition
        return result

    def resolve_llm_materials(
        self, manifest: EvidenceSnapshotManifest | Mapping[str, Any]
    ) -> tuple[bytes, ...]:
        """Resolve bytes after a fresh gate check; this never invokes an LLM."""

        record = self.validate_evidence_manifest(manifest)
        materials: list[bytes] = []
        for item in record.items:
            snapshot = self.repository.get_raw_resource_snapshot(item.snapshot_id)
            materials.append(
                self.blob_store.read_verified(
                    _snapshot_relative_path(snapshot),
                    expected_sha256=item.snapshot_sha256,
                    expected_length=_snapshot_length(snapshot),
                )
            )
        return tuple(materials)

    def _validate_content_snapshot(
        self, snapshot: Any, cutoff: datetime
    ) -> tuple[str | None, str | None]:
        available_at = _get(snapshot, "available_at")
        if not isinstance(available_at, datetime) or not is_point_in_time_eligible(
            available_at, cutoff
        ):
            return "future_available", None
        source_id = str(_get(snapshot, "source_definition_id"))
        source_version = str(_get(snapshot, "source_definition_version"))
        try:
            policy = self.source_policy_resolver(source_id, source_version)
        except Exception:
            return "source_policy_missing", None
        review_status = _live_access_review_value(policy)
        decision = (
            f"{source_id}@{source_version}:review={review_status};"
            f"llm={_llm_policy_value(policy)}"
        )
        if review_status != LiveAccessReviewStatus.APPROVED.value:
            return "source_live_access_not_approved", decision
        if not _formal_evidence_authority_enabled(policy):
            return "source_not_formal_evidence_authority", decision
        if not _llm_allowed(policy):
            return "llm_processing_denied", decision
        if not _archive_allowed(policy):
            return "archive_policy_invalid", decision
        if not str(_get(snapshot, "policy_decision", "")).lower().startswith("allowed"):
            return "snapshot_policy_not_allowed", decision
        try:
            self.blob_store.verify_blob(
                _snapshot_relative_path(snapshot),
                expected_sha256=_snapshot_sha256(snapshot),
                expected_length=_snapshot_length(snapshot),
            )
        except (SnapshotIntegrityMismatch, SnapshotPathEscape):
            return "snapshot_integrity_failed", decision
        try:
            events = self.repository.list_snapshot_integrity_events(
                str(_get(snapshot, "snapshot_id"))
            )
        except Exception:
            events = ()
        if _latest_integrity_status(events) == "quarantined":
            return "snapshot_quarantined", decision
        return None, decision

    def _load_artifacts(self, artifact_ids: Iterable[str]) -> dict[str, list[Any]]:
        grouped: dict[str, list[Any]] = {}
        for artifact_id in sorted(set(artifact_ids)):
            try:
                artifact = self.repository.get_derived_artifact(artifact_id)
            except Exception as exc:
                raise EvidenceGateError(
                    "derived_artifact_missing", f"派生物不存在: {artifact_id}"
                ) from exc
            grouped.setdefault(str(_get(artifact, "parent_snapshot_id")), []).append(
                artifact
            )
        return grouped

    def _validate_artifacts(self, snapshot_id: str, artifacts: Sequence[Any]) -> str | None:
        for artifact in artifacts:
            if str(_get(artifact, "parent_snapshot_id")) != snapshot_id:
                return "derived_parent_mismatch"
            if (_get(artifact, "parameters") or {}).get("parse_reuse") is not None:
                from .mineru import validate_parse_reuse
                try:
                    validate_parse_reuse(self.repository, self.blob_store, artifact)
                except RuntimeError:
                    return "derived_parse_reuse_invalid"
            try:
                self.blob_store.read_verified_derived(
                    _artifact_relative_path(artifact),
                    expected_sha256=_artifact_sha256(artifact),
                    expected_length=_artifact_length(artifact),
                )
            except (SnapshotIntegrityMismatch, SnapshotPathEscape):
                return "derived_integrity_failed"
        return None

    def _validate_audit_snapshot_references(self, snapshot_ids: Sequence[str]) -> None:
        for snapshot_id in snapshot_ids:
            try:
                self.repository.get_raw_resource_snapshot(snapshot_id)
            except Exception as exc:
                raise EvidenceGateError(
                    "audit_reference_missing", f"审计引用快照不存在: {snapshot_id}"
                ) from exc

    def _publish_manifest(self, manifest_hash: str, payload: Mapping[str, Any]) -> datetime:
        destination = _manifest_path(self.blob_store.data_root, manifest_hash)
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = _canonical_json_bytes(payload)
        temp = destination.parent / (
            f".{destination.name}.{os.getpid()}-{secrets.token_hex(12)}.tmp"
        )
        try:
            if destination.exists():
                if destination.read_bytes() != encoded:
                    raise EvidenceGateError(
                        "manifest_file_collision", "同 manifest hash 的文件内容冲突"
                    )
            else:
                with temp.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, destination)
            if destination.read_bytes() != encoded:
                raise EvidenceGateError(
                    "manifest_file_mismatch", "manifest 发布后复核失败"
                )
        except EvidenceGateError:
            _unlink(temp)
            raise
        except OSError as exc:
            _unlink(temp)
            raise EvidenceGateError(
                "manifest_write_failed", "manifest 原子发布失败"
            ) from exc
        return datetime.fromtimestamp(destination.stat().st_mtime, tz=timezone.utc)

    def _read_manifest_payload(self, manifest_hash: str) -> dict[str, Any]:
        path = _manifest_path(self.blob_store.data_root, manifest_hash)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceGateError(
                "manifest_file_missing", "manifest 文件缺失或损坏"
            ) from exc
        if not isinstance(value, dict):
            raise EvidenceGateError(
                "manifest_file_mismatch", "manifest 文件根节点非法"
            )
        return value


def build_evidence_manifest(
    service: EvidenceManifestService, **kwargs: Any
) -> EvidenceSnapshotManifest:
    return service.build_evidence_manifest(**kwargs)


def validate_evidence_manifest(
    service: EvidenceManifestService,
    manifest: EvidenceSnapshotManifest | Mapping[str, Any],
) -> EvidenceSnapshotManifest:
    return service.validate_evidence_manifest(manifest)


def _manifest_payload(
    *,
    storage_namespace_id: str,
    run_id: str,
    question_set_version: str,
    registry_id: str,
    registry_version: str,
    as_of: datetime,
    items: Sequence[EvidenceManifestItem],
    exclusions: Sequence[EvidenceManifestExclusion],
    coverage_summary: Mapping[str, Any],
    policy_decisions: Sequence[str],
    gate_passed: bool,
) -> dict[str, Any]:
    return {
        "schema": "evidence-snapshot-manifest-v1",
        "storage_namespace_id": storage_namespace_id,
        "run_id": run_id,
        "question_set_version": question_set_version,
        "registry_id": registry_id,
        "registry_version": registry_version,
        "as_of": _aware_utc(as_of).isoformat(),
        "items": [_plain(item) for item in sorted(items, key=lambda x: x.snapshot_id)],
        "exclusions": [
            _plain(item)
            for item in sorted(
                exclusions, key=lambda x: (x.object_type, x.object_id, x.reason_code)
            )
        ],
        "coverage_summary": _plain(coverage_summary),
        "policy_decisions": sorted(set(policy_decisions)),
        "gate_passed": gate_passed,
    }


def _exclusion(object_type: str, object_id: str, reason_code: str) -> EvidenceManifestExclusion:
    return EvidenceManifestExclusion(
        object_type=object_type, object_id=object_id, reason_code=reason_code
    )


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {
            str(key): _plain(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


def _get(value: Any, field: str, default: Any = None) -> Any:
    return value.get(field, default) if isinstance(value, Mapping) else getattr(value, field, default)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _snapshot_sha256(value: Any) -> str:
    return str(_get(value, "sha256", _get(value, "content_sha256", "")))


def _snapshot_length(value: Any) -> int:
    return int(_get(value, "byte_length", _get(value, "content_length", -1)))


def _snapshot_relative_path(value: Any) -> str:
    return str(_get(value, "archive_relative_path", _get(value, "relative_path", "")))


def _artifact_id(value: Any) -> str:
    return str(_get(value, "derived_artifact_id", _get(value, "artifact_id", "")))


def _artifact_sha256(value: Any) -> str:
    return str(_get(value, "output_sha256", _get(value, "sha256", "")))


def _artifact_length(value: Any) -> int | None:
    length = _get(value, "output_byte_length", _get(value, "byte_length"))
    return None if length is None else int(length)


def _artifact_relative_path(value: Any) -> str:
    return str(_get(value, "archive_relative_path", _get(value, "relative_path", "")))


def _latest_integrity_status(events: Sequence[Any]) -> str | None:
    if not events:
        return None
    latest = max(
        events,
        key=lambda event: (
            str(_get(event, "checked_at", _get(event, "created_at", ""))),
            str(_get(event, "integrity_event_id", _get(event, "event_id", ""))),
        ),
    )
    return _enum_value(_get(latest, "status", _get(latest, "result")))


def _license_policy(policy: Any) -> Any:
    return _get(policy, "license_policy", policy)


def _retention_policy(policy: Any) -> Any:
    return _get(policy, "retention_policy", policy)


def _llm_policy_value(policy: Any) -> str:
    return _enum_value(_get(_license_policy(policy), "llm_processing"))


def _live_access_review_value(policy: Any) -> str:
    review = _get(policy, "live_access_review", {})
    return _enum_value(_get(review, "status"))


def _formal_evidence_authority_enabled(policy: Any) -> bool:
    return (
        _get(policy, "enabled") is True
        and _enum_value(_get(policy, "policy_status"))
        == SourcePolicyStatus.ENABLED.value
        and _enum_value(_get(policy, "access_method"))
        in {"https_api", "https_document"}
    )


def _llm_allowed(policy: Any) -> bool:
    value = _get(_license_policy(policy), "llm_processing")
    return value is True or _enum_value(value) == PolicyDecision.ALLOWED.value


def _archive_allowed(policy: Any) -> bool:
    license_value = _get(_license_policy(policy), "archive_original", True)
    retention_value = _get(_retention_policy(policy), "content_body", True)
    return _allowed(license_value) and _allowed(retention_value)


def _allowed(value: Any) -> bool:
    return value is True or _enum_value(value) in {
        PolicyDecision.ALLOWED.value,
        "allow",
        "retain",
    }


def _manifest_path(data_root: Path, digest: str) -> Path:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("manifest hash 必须是完整 SHA-256")
    destination = (data_root / "acquisition" / "evidence" / f"{digest}.json").resolve()
    try:
        destination.relative_to(data_root)
    except ValueError as exc:
        raise SnapshotPathEscape("manifest 路径越出绑定 data_root") from exc
    return destination


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _aware_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
