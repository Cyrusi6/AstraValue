from __future__ import annotations

import hmac
import ipaddress
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from threading import RLock
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

from pydantic import Field, model_validator

from .canonical import canonical_json_bytes, canonical_sha256
from .codex_tools import CodexToolError, InMemoryCodexSessionRecorder
from .models import (
    AuthoritativeSourceCandidate,
    ContextualEvidenceItem,
    GovernanceModel,
    GovernancePerspective,
    GovernanceSnapshot,
    QuarantinedResearchItem,
    QuarantineReason,
    ResearchBudget,
    BudgetUsage,
    ResearchResultBundle,
    ResearchTask,
    ResearchTaskStatus,
    SnapshotAdoption,
    SourceRole,
)


class ResearchIntegrityError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _hash(schema_name: str, payload: Any, version: str = "1.0.0") -> str:
    return canonical_sha256(
        payload,
        schema_name=schema_name,
        schema_version=version,
    )


def _opaque_id(
    namespace: str,
    schema_name: str,
    payload: Any,
    version: str = "1.0.0",
) -> str:
    """Derive a parent-owned opaque identity from canonical semantic input."""

    return f"{namespace}:{_hash(schema_name, payload, version)}"


def _opaque_research_item_id(
    bundle_hash: str,
    item: GovernanceModel,
) -> str:
    return _opaque_id(
        "govresearchitem",
        "governance-parent-research-item-identity",
        {
            "bundle_hash": bundle_hash,
            "item_schema_name": item.schema_name,
            "item": item.model_dump(mode="python"),
        },
        str(getattr(item, "schema_version", "1.0.0")),
    )


def research_task_hash(task: ResearchTask) -> str:
    return _hash(
        task.schema_name,
        task.model_dump(mode="python", exclude={"canonical_hash"}),
        task.schema_version,
    )


def research_result_bundle_hash(bundle: ResearchResultBundle) -> str:
    return _hash(
        bundle.schema_name,
        bundle.model_dump(mode="python", exclude={"canonical_hash"}),
        bundle.schema_version,
    )


def seal_research_result_bundle(bundle: ResearchResultBundle) -> ResearchResultBundle:
    """Return the same immutable result with its real semantic hash."""

    return ResearchResultBundle.model_validate(
        {
            **bundle.model_dump(mode="python"),
            "canonical_hash": research_result_bundle_hash(bundle),
        },
        strict=True,
    )


class ReingestedEvidence(GovernanceModel):
    """Sanitized identity returned after a formal candidate re-enters acquisition."""

    schema_name = "governance-reingested-evidence"
    kind: Literal["reingested_evidence"] = "reingested_evidence"
    source_item_id: str
    acquisition_run_id: str
    evidence_manifest_id: str
    evidence_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    governance_snapshot_id: str | None = None
    governance_snapshot_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_snapshot_pair(self) -> "ReingestedEvidence":
        if (self.governance_snapshot_id is None) != (
            self.governance_snapshot_hash is None
        ):
            raise ValueError("reingested snapshot ID/hash must be supplied together")
        return self


class AuthoritativeReingestor(Protocol):
    def reingest(
        self,
        task: ResearchTask,
        candidate: AuthoritativeSourceCandidate,
    ) -> ReingestedEvidence: ...


class AuthoritativeSourcePolicy(Protocol):
    """Versioned allowlist consulted before any authoritative reingestion."""

    policy_version: str

    def allows(
        self,
        *,
        source_role: SourceRole,
        scheme: str,
        hostname: str,
        port: int | None,
        path: str,
    ) -> bool: ...


class SanitizedResearchReceipt(GovernanceModel):
    """The only research result that a parent Codex may receive."""

    schema_name = "governance-sanitized-research-receipt"
    kind: Literal["sanitized_research_receipt"] = "sanitized_research_receipt"
    schema_version: str = "1.0.0"
    receipt_id: str
    research_task_id: str
    research_result_bundle_id: str
    task_status: ResearchTaskStatus
    reingested_evidence: tuple[ReingestedEvidence, ...] = ()
    eligible_contextual_artifact_ids: tuple[str, ...] = ()
    discovery_lead_ids: tuple[str, ...] = ()
    unresolved_gap_ids: tuple[str, ...] = ()
    quarantine_count: int = Field(ge=0)
    quarantine_reason_counts: tuple[tuple[str, int], ...] = ()
    deferred_count: int = Field(ge=0)
    budget_exhausted: bool
    failure_reason_code: str | None = None
    canonical_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> "SanitizedResearchReceipt":
        if not self.receipt_id.startswith("govresearchreceipt:"):
            raise ValueError("receipt_id must use the govresearchreceipt namespace")
        if self.budget_exhausted != (
            self.task_status == ResearchTaskStatus.BUDGET_EXHAUSTED
        ):
            raise ValueError("budget_exhausted must match task status")
        if self.task_status == ResearchTaskStatus.FAILED and not self.failure_reason_code:
            raise ValueError("failed receipt requires a non-sensitive reason code")
        for values in (
            self.eligible_contextual_artifact_ids,
            self.discovery_lead_ids,
            self.unresolved_gap_ids,
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError("receipt identifiers must be unique and sorted")
        if tuple(sorted(self.quarantine_reason_counts)) != self.quarantine_reason_counts:
            raise ValueError("quarantine reason counts must be sorted")
        return self


class InMemoryResearchStaging:
    """Capability-separated staging: callers receive IDs, never raw bundle payloads."""

    def __init__(self) -> None:
        self._bundles: dict[str, ResearchResultBundle] = {}
        self._quarantine: dict[str, bytes] = {}
        self._eligible_contextual: dict[str, ContextualEvidenceItem] = {}

    def stage(self, bundle: ResearchResultBundle) -> str:
        artifact_id = _opaque_id(
            "research-staging",
            "governance-research-staging-identity",
            {"bundle_hash": research_result_bundle_hash(bundle)},
        )
        existing = self._bundles.get(artifact_id)
        if existing is not None and existing != bundle:
            raise ResearchIntegrityError(
                "immutable_conflict", "staging artifact ID already has different content"
            )
        self._bundles[artifact_id] = bundle
        return artifact_id

    def _load_for_gate(self, artifact_id: str) -> ResearchResultBundle:
        try:
            return self._bundles[artifact_id]
        except KeyError as exc:
            raise ResearchIntegrityError(
                "bundle_not_found", "research staging artifact not found"
            ) from exc

    def quarantine(self, artifact_id: str, payload: Any) -> str:
        raw = canonical_json_bytes(payload)
        existing = self._quarantine.get(artifact_id)
        if existing is not None and existing != raw:
            raise ResearchIntegrityError(
                "immutable_conflict", "quarantine artifact identity collision"
            )
        self._quarantine[artifact_id] = raw
        return _hash("governance-research-quarantine-payload", payload)

    def admit_contextual(
        self,
        item: ContextualEvidenceItem,
        *,
        source_item_id: str,
    ) -> str:
        artifact_id = _opaque_id(
            "research-contextual",
            "governance-contextual-research-artifact-identity",
            {
                "source_item_id": source_item_id,
                "item_schema_name": item.schema_name,
                "item": item.model_dump(mode="python"),
            },
            str(getattr(item, "schema_version", "1.0.0")),
        )
        existing = self._eligible_contextual.get(artifact_id)
        if existing is not None and existing != item:
            raise ResearchIntegrityError(
                "immutable_conflict", "contextual artifact identity collision"
            )
        self._eligible_contextual[artifact_id] = item
        return artifact_id


def validate_budget_contract(task: ResearchTask, bundle: ResearchResultBundle) -> None:
    usage = bundle.budget_used
    budget = task.budget
    limits = {
        "rounds": budget.max_rounds,
        "child_tasks": budget.max_child_tasks,
        "network_requests": budget.max_network_requests,
        "wall_clock": budget.wall_clock_seconds * 1000,
        "output_bytes": budget.max_output_bytes,
    }
    values = {
        "rounds": usage.rounds,
        "child_tasks": usage.child_tasks,
        "network_requests": usage.network_requests,
        "wall_clock": usage.wall_clock_milliseconds,
        "output_bytes": usage.output_bytes,
    }
    overages = {name for name, value in values.items() if value > limits[name]}

    def reject(detail: str) -> None:
        raise ResearchIntegrityError("budget_contract_violation", detail)

    if bundle.task_status == ResearchTaskStatus.BUDGET_EXHAUSTED:
        reason = bundle.failure_reason
        known_reasons = {
            "budget_exhausted",
            "budget_unavailable",
            "network_request_budget",
            "parallelism_budget",
            "stream_size_limit",
            "wall_clock_budget",
        }
        if reason not in known_reasons:
            reject("budget exhaustion reason is unknown")
        if reason in {"budget_exhausted", "budget_unavailable"}:
            if overages or any(values.values()):
                reject("unavailable budget must report zero usage")
            if not any(
                (
                    budget.max_rounds == 0,
                    budget.max_child_tasks == 0,
                    budget.max_network_requests == 0,
                )
            ):
                reject("unavailable budget reason does not match the task budget")
        elif reason == "parallelism_budget":
            if overages.difference({"wall_clock"}) or any(
                (
                    usage.rounds,
                    usage.child_tasks,
                    usage.network_requests,
                    usage.output_bytes,
                )
            ):
                reject("parallelism exhaustion must not report child work")
        elif reason == "wall_clock_budget":
            if overages.difference({"wall_clock"}) or (
                usage.wall_clock_milliseconds < limits["wall_clock"]
            ):
                reject("wall-clock exhaustion does not match observed usage")
        elif reason == "network_request_budget":
            if overages != {"network_requests"}:
                reject("network exhaustion does not match observed usage")
        elif reason == "stream_size_limit":
            if overages != {"output_bytes"}:
                reject("output exhaustion does not match observed usage")
        return

    if overages:
        reject(f"budget limit exceeded: {','.join(sorted(overages))}")
    if usage.child_tasks > 0 and budget.max_parallelism < 1:
        raise ResearchIntegrityError(
            "budget_contract_violation", "parallelism contract is invalid"
        )


class ResearchGate:
    def __init__(
        self,
        staging: InMemoryResearchStaging,
        *,
        authoritative_reingestor: AuthoritativeReingestor | None = None,
        authoritative_source_policy: AuthoritativeSourcePolicy | None = None,
    ) -> None:
        self._staging = staging
        self._reingestor = authoritative_reingestor
        self._source_policy = authoritative_source_policy
        self._quarantine_records: list[QuarantinedResearchItem] = []

    @property
    def quarantine_records(self) -> tuple[QuarantinedResearchItem, ...]:
        return tuple(self._quarantine_records)

    def process(
        self,
        *,
        task: ResearchTask,
        staged_bundle_artifact_id: str,
        gated_at: datetime,
    ) -> SanitizedResearchReceipt:
        if not hmac.compare_digest(task.canonical_hash, research_task_hash(task)):
            raise ResearchIntegrityError("task_hash_mismatch", "research task hash mismatch")
        bundle = self._staging._load_for_gate(staged_bundle_artifact_id)
        if bundle.research_task_id != task.research_task_id:
            raise ResearchIntegrityError(
                "task_binding_mismatch", "bundle does not belong to the research task"
            )
        if not hmac.compare_digest(
            bundle.canonical_hash, research_result_bundle_hash(bundle)
        ):
            raise ResearchIntegrityError(
                "bundle_hash_mismatch", "research result bundle hash mismatch"
            )
        validate_budget_contract(task, bundle)
        reported_gap_ids = {item.gap_id for item in bundle.unresolved_gaps}
        if not reported_gap_ids.issubset(set(task.gap_ids)):
            raise ResearchIntegrityError(
                "gap_binding_mismatch",
                "research result contains a gap outside the parent task",
            )

        opaque_bundle_id = (
            f"govresearchbundle:{research_result_bundle_hash(bundle)}"
        )

        reingested: list[ReingestedEvidence] = []
        contextual_artifacts: list[str] = []
        discovery_ids: list[str] = []
        unresolved_gap_ids: list[str] = []
        reasons: Counter[str] = Counter()

        for candidate in bundle.authoritative_source_candidates:
            reason = self._temporal_or_policy_reason(task, candidate)
            opaque_item_id = _opaque_research_item_id(
                bundle.canonical_hash,
                candidate,
            )
            if reason is not None:
                self._quarantine(
                    task,
                    opaque_bundle_id,
                    opaque_item_id,
                    candidate,
                    reason,
                    gated_at,
                )
                reasons[reason.value] += 1
                continue
            if self._reingestor is None:
                # A URL or child narrative is never itself admitted as evidence.
                unresolved_gap_ids.extend(task.gap_ids)
                continue
            result = self._reingestor.reingest(task, candidate)
            reingested.append(
                ReingestedEvidence.model_validate(
                    {
                        **result.model_dump(mode="python"),
                        "source_item_id": opaque_item_id,
                    },
                    strict=True,
                )
            )

        for item in bundle.contextual_evidence:
            reason = self._temporal_or_policy_reason(task, item)
            opaque_item_id = _opaque_research_item_id(bundle.canonical_hash, item)
            if reason is not None:
                self._quarantine(
                    task,
                    opaque_bundle_id,
                    opaque_item_id,
                    item,
                    reason,
                    gated_at,
                )
                reasons[reason.value] += 1
                continue
            contextual_artifacts.append(
                self._staging.admit_contextual(
                    item,
                    source_item_id=opaque_item_id,
                )
            )

        # Discovery IDs carry no title, URL, number, or conclusion into the parent.
        if SourceRole.DISCOVERY_ONLY in task.allowed_source_roles:
            discovery_ids.extend(
                _opaque_id(
                    "research-discovery",
                    "governance-discovery-lead-identity",
                    {
                        "source_item_id": _opaque_research_item_id(
                            bundle.canonical_hash,
                            item,
                        ),
                        "item": item.model_dump(mode="python"),
                    },
                    str(getattr(item, "schema_version", "1.0.0")),
                )
                for item in bundle.discovery_leads
            )
        elif bundle.discovery_leads:
            for item in bundle.discovery_leads:
                opaque_item_id = _opaque_research_item_id(bundle.canonical_hash, item)
                self._quarantine(
                    task,
                    opaque_bundle_id,
                    opaque_item_id,
                    item,
                    QuarantineReason.SOURCE_POLICY,
                    gated_at,
                )
                reasons[QuarantineReason.SOURCE_POLICY.value] += 1

        for item in bundle.deferred_items:
            opaque_item_id = _opaque_research_item_id(bundle.canonical_hash, item)
            self._quarantine(
                task,
                opaque_bundle_id,
                opaque_item_id,
                item,
                QuarantineReason.DEFERRED_SOURCE,
                gated_at,
            )
            reasons[QuarantineReason.DEFERRED_SOURCE.value] += 1
        unresolved_gap_ids.extend(item.gap_id for item in bundle.unresolved_gaps)
        if bundle.task_status in {
            ResearchTaskStatus.FAILED,
            ResearchTaskStatus.BUDGET_EXHAUSTED,
        }:
            unresolved_gap_ids.extend(task.gap_ids)

        payload = {
            "research_task_id": task.research_task_id,
            "research_result_bundle_id": opaque_bundle_id,
            "task_status": bundle.task_status,
            "reingested_evidence": tuple(
                sorted(reingested, key=lambda item: item.source_item_id)
            ),
            "eligible_contextual_artifact_ids": tuple(
                sorted(set(contextual_artifacts))
            ),
            "discovery_lead_ids": tuple(sorted(set(discovery_ids))),
            "unresolved_gap_ids": tuple(sorted(set(unresolved_gap_ids))),
            "quarantine_count": sum(reasons.values()),
            "quarantine_reason_counts": tuple(sorted(reasons.items())),
            "deferred_count": reasons[QuarantineReason.DEFERRED_SOURCE.value],
            "budget_exhausted": bundle.task_status
            == ResearchTaskStatus.BUDGET_EXHAUSTED,
            "failure_reason_code": (
                "research_task_failed"
                if bundle.task_status == ResearchTaskStatus.FAILED
                else None
            ),
        }
        digest = _hash("governance-sanitized-research-receipt", payload)
        return SanitizedResearchReceipt(
            receipt_id=f"govresearchreceipt:{digest}",
            **payload,
            canonical_hash=digest,
        )

    def _temporal_or_policy_reason(
        self,
        task: ResearchTask,
        item: AuthoritativeSourceCandidate | ContextualEvidenceItem,
    ) -> QuarantineReason | None:
        if item.source_role not in task.allowed_source_roles:
            return QuarantineReason.SOURCE_POLICY
        if isinstance(item, AuthoritativeSourceCandidate) and not self._allows_locator(
            item
        ):
            return QuarantineReason.SOURCE_POLICY
        if item.available_at is None:
            return QuarantineReason.AVAILABLE_AT_UNPROVEN
        if item.available_at > task.known_at:
            return QuarantineReason.FUTURE_INFORMATION
        return None

    def _allows_locator(self, item: AuthoritativeSourceCandidate) -> bool:
        policy = self._source_policy
        if policy is None:
            return False
        policy_version = getattr(policy, "policy_version", None)
        if not isinstance(policy_version, str) or not policy_version.strip():
            return False
        locator = item.source_locator
        if any(ord(character) < 32 for character in locator):
            return False
        try:
            parsed = urlsplit(locator)
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname
            port = parsed.port
            username = parsed.username
            password = parsed.password
        except (TypeError, ValueError, UnicodeError):
            return False
        if scheme not in {"http", "https"} or not hostname:
            return False
        if username is not None or password is not None or parsed.fragment:
            return False
        try:
            normalized_hostname = (
                hostname.rstrip(".").encode("idna").decode("ascii").lower()
            )
        except UnicodeError:
            return False
        if not normalized_hostname or normalized_hostname == "localhost" or (
            normalized_hostname.endswith(".localhost")
        ):
            return False
        try:
            address = ipaddress.ip_address(normalized_hostname)
        except ValueError:
            # Reject alternative numeric spellings such as 127.1 or 2130706433;
            # a registered DNS hostname must contain at least one non-numeric label.
            if re.fullmatch(r"(?:0x)?[0-9a-f.]+", normalized_hostname, re.I):
                return False
        else:
            if not address.is_global:
                return False
        raw_path = parsed.path or "/"
        try:
            decoded_path = unquote(raw_path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return False
        if "\\" in raw_path or "\\" in decoded_path or any(
            ord(character) < 32 for character in decoded_path
        ):
            return False
        try:
            allowed = policy.allows(
                source_role=item.source_role,
                scheme=scheme,
                hostname=normalized_hostname,
                port=port,
                path=raw_path,
            )
        except Exception:
            return False
        return allowed is True

    def _quarantine(
        self,
        task: ResearchTask,
        opaque_bundle_id: str,
        opaque_item_id: str,
        item: GovernanceModel,
        reason: QuarantineReason,
        gated_at: datetime,
    ) -> None:
        payload = item.model_dump(mode="python")
        artifact_id = _opaque_id(
            "research-quarantine",
            "governance-research-quarantine-artifact-identity",
            {
                "bundle_id": opaque_bundle_id,
                "source_item_id": opaque_item_id,
                "payload": payload,
            },
            str(getattr(item, "schema_version", "1.0.0")),
        )
        payload_hash = self._staging.quarantine(artifact_id, payload)
        record_payload = {
            "research_task_id": task.research_task_id,
            "research_result_bundle_id": opaque_bundle_id,
            "source_item_id": opaque_item_id,
            "reason_code": reason,
            "quarantined_payload_artifact_id": artifact_id,
            "quarantined_payload_hash": payload_hash,
            "known_at": task.known_at,
            "quarantined_at": gated_at,
        }
        digest = _hash("governance-quarantined-research-item", record_payload)
        self._quarantine_records.append(
            QuarantinedResearchItem(
                quarantine_id=f"govquarantine:{digest}",
                **record_payload,
                canonical_hash=digest,
            )
        )


class SnapshotCatalog(Protocol):
    def get_snapshot(self, snapshot_id: str) -> GovernanceSnapshot: ...


class SnapshotIntegrityVerifier(Protocol):
    """Full snapshot/manifest integrity verification supplied by persistence."""

    def verify(self, snapshot: GovernanceSnapshot) -> None: ...


class InMemorySnapshotCatalog:
    def __init__(self, snapshots: Sequence[GovernanceSnapshot] = ()) -> None:
        self._snapshots = {item.governance_snapshot_id: item for item in snapshots}

    def add(self, snapshot: GovernanceSnapshot) -> None:
        existing = self._snapshots.get(snapshot.governance_snapshot_id)
        if existing is not None and existing != snapshot:
            raise ResearchIntegrityError(
                "immutable_conflict", "snapshot identity already has different content"
            )
        self._snapshots[snapshot.governance_snapshot_id] = snapshot

    def get_snapshot(self, snapshot_id: str) -> GovernanceSnapshot:
        try:
            return self._snapshots[snapshot_id]
        except KeyError as exc:
            raise ResearchIntegrityError("snapshot_not_found", "snapshot not found") from exc


class SnapshotAdoptionService:
    def __init__(
        self,
        catalog: SnapshotCatalog,
        recorder: InMemoryCodexSessionRecorder,
        integrity_verifier: SnapshotIntegrityVerifier,
    ) -> None:
        self._catalog = catalog
        self._recorder = recorder
        self._integrity_verifier = integrity_verifier

    def adopt_snapshot(
        self,
        *,
        session_id: str,
        new_snapshot_id: str,
        expected_revision: int,
        adopted_at_sequence: int,
        adopted_at: datetime,
    ) -> SnapshotAdoption:
        old_snapshot_id, old_snapshot_hash, revision = self._recorder.current_binding(
            session_id
        )
        if revision != expected_revision:
            raise CodexToolError("revision_conflict", "session revision has changed")
        old = self._catalog.get_snapshot(old_snapshot_id)
        new = self._catalog.get_snapshot(new_snapshot_id)
        self._integrity_verifier.verify(old)
        self._integrity_verifier.verify(new)
        if old.canonical_snapshot_hash != old_snapshot_hash:
            raise ResearchIntegrityError(
                "snapshot_hash_mismatch", "current snapshot binding hash mismatch"
            )
        if (
            old.company_id != new.company_id
            or old.acquisition_scope != new.acquisition_scope
            or old.question_set_id != new.question_set_id
        ):
            raise ResearchIntegrityError(
                "snapshot_namespace_mismatch", "snapshot adoption cannot cross namespace"
            )
        if (
            old.state_at != new.state_at
            or old.known_at != new.known_at
            or old.perspective != new.perspective
        ):
            raise ResearchIntegrityError(
                "snapshot_time_mismatch", "adopted snapshot must preserve query semantics"
            )
        if new.supersedes_snapshot_id != old.governance_snapshot_id:
            raise ResearchIntegrityError(
                "snapshot_lineage_mismatch", "new snapshot does not extend the session snapshot"
            )
        payload = {
            "session_id": session_id,
            "expected_session_revision": expected_revision,
            "resulting_session_revision": expected_revision + 1,
            "old_snapshot_id": old.governance_snapshot_id,
            "old_snapshot_hash": old.canonical_snapshot_hash,
            "new_snapshot_id": new.governance_snapshot_id,
            "new_snapshot_hash": new.canonical_snapshot_hash,
            "adopted_at_sequence": adopted_at_sequence,
            "adopted_at": adopted_at,
            "temporal_gate_passed": True,
            "lineage_gate_passed": True,
        }
        digest = _hash("governance-snapshot-adoption", payload)
        adoption = SnapshotAdoption(
            snapshot_adoption_id=f"govadoption:{digest}",
            **payload,
            canonical_hash=digest,
        )
        self._recorder.append_adoption(adoption)
        return adoption


class ResearchBrokerPort(Protocol):
    def submit(self, task: ResearchTask) -> ResearchResultBundle: ...


class ResearchCoordinator:
    """Budgeted parent-side orchestration; only sanitized receipts escape the gate."""

    def __init__(
        self,
        *,
        broker: ResearchBrokerPort,
        staging: InMemoryResearchStaging,
        gate: ResearchGate,
        recorder: InMemoryCodexSessionRecorder,
        result_schema_hash: str,
    ) -> None:
        self._broker = broker
        self._staging = staging
        self._gate = gate
        self._recorder = recorder
        self._result_schema_hash = result_schema_hash
        self._used: dict[str, BudgetUsage] = {}
        self._budget_lock = RLock()

    @staticmethod
    def _zero_usage() -> BudgetUsage:
        return BudgetUsage(
            rounds=0,
            child_tasks=0,
            network_requests=0,
            wall_clock_milliseconds=0,
            output_bytes=0,
        )

    def _session_is_finalized(self, session_id: str) -> bool:
        checker = getattr(self._recorder, "is_finalized", None)
        if callable(checker):
            return bool(checker(session_id))
        # The in-memory reference recorder exposes finalized state through its
        # immutable manifest.  Durable recorders should implement is_finalized
        # so this check can be made against their persisted session transaction.
        manifest_reader = getattr(self._recorder, "manifest", None)
        if callable(manifest_reader):
            status = manifest_reader(session_id).generation_status
            return getattr(status, "value", status) in {"completed", "failed"}
        return False

    def _ensure_session_open(self, session_id: str) -> None:
        if self._session_is_finalized(session_id):
            raise ResearchIntegrityError(
                "session_finalized",
                "a finalized Codex session cannot start new research",
            )

    def _remaining_budget(self, session_id: str) -> ResearchBudget:
        budget = self._recorder.input_pack(session_id).research_budget
        used = self._used.get(session_id, self._zero_usage())
        return ResearchBudget(
            max_rounds=max(0, budget.max_rounds - used.rounds),
            max_child_tasks=max(0, budget.max_child_tasks - used.child_tasks),
            max_network_requests=max(
                0, budget.max_network_requests - used.network_requests
            ),
            max_parallelism=budget.max_parallelism,
            wall_clock_seconds=max(
                1, budget.wall_clock_seconds - used.wall_clock_milliseconds // 1000
            ),
            max_output_bytes=max(1, budget.max_output_bytes - used.output_bytes),
            max_recursion_depth=1,
        )

    def _budget_is_exhausted(self, session_id: str) -> bool:
        budget = self._recorder.input_pack(session_id).research_budget
        used = self._used.get(session_id)
        if used is None:
            return False
        return any(
            (
                used.rounds >= budget.max_rounds,
                used.child_tasks >= budget.max_child_tasks,
                used.network_requests >= budget.max_network_requests,
                used.wall_clock_milliseconds >= budget.wall_clock_seconds * 1000,
                used.output_bytes >= budget.max_output_bytes,
            )
        )

    def _reserve_attempt(self, session_id: str) -> None:
        previous = self._used.get(session_id, self._zero_usage())
        self._used[session_id] = BudgetUsage(
            rounds=previous.rounds + 1,
            child_tasks=previous.child_tasks + 1,
            network_requests=previous.network_requests,
            wall_clock_milliseconds=previous.wall_clock_milliseconds,
            output_bytes=previous.output_bytes,
        )

    def _record_actual_usage(
        self,
        session_id: str,
        usage: BudgetUsage,
        *,
        attempt_reserved: bool,
    ) -> None:
        with self._budget_lock:
            previous = self._used.get(session_id, self._zero_usage())
            reserved_rounds = 1 if attempt_reserved else 0
            reserved_children = 1 if attempt_reserved else 0
            counted_rounds = max(reserved_rounds, usage.rounds)
            counted_children = max(reserved_children, usage.child_tasks)
            self._used[session_id] = BudgetUsage(
                rounds=previous.rounds + counted_rounds - reserved_rounds,
                child_tasks=(
                    previous.child_tasks + counted_children - reserved_children
                ),
                network_requests=(
                    previous.network_requests + usage.network_requests
                ),
                wall_clock_milliseconds=(
                    previous.wall_clock_milliseconds
                    + usage.wall_clock_milliseconds
                ),
                output_bytes=previous.output_bytes + usage.output_bytes,
            )

    def request(
        self,
        *,
        session_id: str,
        question_ids: Sequence[str],
        gap_ids: Sequence[str],
        question: str,
        known_evidence_ids: Sequence[str] = (),
        allowed_source_roles: Sequence[SourceRole],
        requested_at: datetime,
    ) -> SanitizedResearchReceipt:
        with self._budget_lock:
            self._ensure_session_open(session_id)
            input_pack = self._recorder.input_pack(session_id)
            remaining = self._remaining_budget(session_id)
            exhausted = self._budget_is_exhausted(session_id) or any(
                (
                    remaining.max_rounds == 0,
                    remaining.max_child_tasks == 0,
                    remaining.max_network_requests == 0,
                )
            )
            task_budget = remaining
            if exhausted and not any(
                (
                    remaining.max_rounds == 0,
                    remaining.max_child_tasks == 0,
                    remaining.max_network_requests == 0,
                )
            ):
                # ResearchBudget deliberately has positive wall/output minima.
                # A terminal coordinator-owned task therefore receives zero
                # rounds to represent that no new work was authorized after a
                # cumulative wall/output limit had already been consumed.
                task_budget = ResearchBudget(
                    max_rounds=0,
                    max_child_tasks=remaining.max_child_tasks,
                    max_network_requests=remaining.max_network_requests,
                    max_parallelism=remaining.max_parallelism,
                    wall_clock_seconds=remaining.wall_clock_seconds,
                    max_output_bytes=remaining.max_output_bytes,
                    max_recursion_depth=1,
                )
            task = create_research_task(
                parent_session_id=session_id,
                company_id=input_pack.company_id,
                question_ids=question_ids,
                gap_ids=gap_ids,
                question=question,
                state_at=input_pack.state_at,
                known_at=input_pack.known_at,
                perspective=input_pack.perspective,
                known_evidence_ids=known_evidence_ids,
                allowed_source_roles=allowed_source_roles,
                budget=task_budget,
                result_schema_hash=self._result_schema_hash,
                created_at=requested_at,
            )
            attempt_reserved = not exhausted
            if attempt_reserved:
                # Reserve the irreducible cost before another caller can inspect
                # the same session budget. Actual measured usage is reconciled
                # after the broker returns, while this reservation is retained
                # even when the broker or gate fails.
                self._reserve_attempt(session_id)

        if exhausted:
            bundle = ResearchResultBundle(
                research_result_bundle_id=(
                    f"govresearchbundle:{task.research_task_id.split(':', 1)[1]}:budget"
                ),
                research_task_id=task.research_task_id,
                task_status=ResearchTaskStatus.BUDGET_EXHAUSTED,
                budget_used=BudgetUsage(
                    rounds=0,
                    child_tasks=0,
                    network_requests=0,
                    wall_clock_milliseconds=0,
                    output_bytes=0,
                ),
                unresolved_gaps=(),
                failure_reason="budget_exhausted",
                created_at=requested_at,
                canonical_hash="0" * 64,
            )
            bundle = seal_research_result_bundle(bundle)
        else:
            try:
                bundle = self._broker.submit(task)
            except ResearchIntegrityError:
                raise
            except Exception:
                bundle = ResearchResultBundle(
                    research_result_bundle_id=(
                        f"govresearchbundle:{task.research_task_id.split(':', 1)[1]}:failed"
                    ),
                    research_task_id=task.research_task_id,
                    task_status=ResearchTaskStatus.FAILED,
                    budget_used=BudgetUsage(
                        rounds=1,
                        child_tasks=1,
                        network_requests=0,
                        wall_clock_milliseconds=0,
                        output_bytes=0,
                    ),
                    failure_reason="broker_failed",
                    created_at=requested_at,
                    canonical_hash="0" * 64,
                )
                bundle = seal_research_result_bundle(bundle)
        self._record_actual_usage(
            session_id,
            bundle.budget_used,
            attempt_reserved=attempt_reserved,
        )
        artifact_id = self._staging.stage(bundle)
        receipt = self._gate.process(
            task=task,
            staged_bundle_artifact_id=artifact_id,
            gated_at=requested_at,
        )
        self._recorder.append_research_result(
            session_id,
            task.research_task_id,
            receipt.research_result_bundle_id,
        )
        return receipt


def create_research_task(
    *,
    parent_session_id: str,
    company_id: str,
    question_ids: Sequence[str],
    gap_ids: Sequence[str],
    question: str,
    state_at: datetime,
    known_at: datetime,
    perspective: GovernancePerspective,
    known_evidence_ids: Sequence[str],
    allowed_source_roles: Sequence[SourceRole],
    budget: ResearchBudget,
    result_schema_hash: str,
    created_at: datetime,
) -> ResearchTask:
    payload = {
        "parent_session_id": parent_session_id,
        "company_id": company_id,
        "question_ids": tuple(sorted(set(question_ids))),
        "gap_ids": tuple(sorted(set(gap_ids))),
        "question": question,
        "state_at": state_at,
        "known_at": known_at,
        "perspective": perspective,
        "known_evidence_ids": tuple(sorted(set(known_evidence_ids))),
        "allowed_source_roles": tuple(
            sorted(set(allowed_source_roles), key=lambda role: role.value)
        ),
        "budget": budget,
        "recursion_depth": 1,
        "result_schema_name": "governance-research-result-bundle",
        "result_schema_version": "1.0.0",
        "result_schema_hash": result_schema_hash,
        "created_at": created_at,
    }
    seed = _hash("governance-research-task-identity", payload)
    provisional = ResearchTask(
        research_task_id=f"govresearchtask:{seed}",
        **payload,
        canonical_hash="0" * 64,
    )
    digest = research_task_hash(provisional)
    return ResearchTask.model_validate(
        {**provisional.model_dump(mode="python"), "canonical_hash": digest},
        strict=True,
    )


__all__ = [
    "AuthoritativeReingestor",
    "AuthoritativeSourcePolicy",
    "InMemoryResearchStaging",
    "InMemorySnapshotCatalog",
    "ReingestedEvidence",
    "ResearchGate",
    "ResearchCoordinator",
    "ResearchIntegrityError",
    "SanitizedResearchReceipt",
    "SnapshotIntegrityVerifier",
    "SnapshotAdoptionService",
    "create_research_task",
    "research_result_bundle_hash",
    "research_task_hash",
    "seal_research_result_bundle",
    "validate_budget_contract",
]
