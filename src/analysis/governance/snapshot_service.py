from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pydantic import ValidationError

from .canonical import canonical_json_text, canonical_sha256
from .models import (
    CompletenessStatus,
    DeltaDisposition,
    GovernanceClaim,
    GovernanceEvidenceSpan,
    GovernancePerspective,
    GovernanceQuestionCoverageLink,
    GovernanceRecord,
    GovernanceSnapshot,
    OwnershipPosition,
    OwnershipSnapshot,
    SnapshotAnchorLink,
    SnapshotDeltaLink,
    SnapshotRecordLink,
    SnapshotRecordRole,
    TimePrecision,
)


class SnapshotIntegrityError(RuntimeError):
    """A hard failure that prevents publication of a governance snapshot."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SnapshotAnchorInput:
    question_id: str
    state_kind: str
    anchor_record_id: str
    reference_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class SnapshotDeltaInput:
    question_id: str
    delta_record_id: str
    disposition: DeltaDisposition
    effective_at: datetime
    available_at: datetime
    announced_at: datetime | None = None
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class SnapshotBuildRequest:
    namespace: str
    company_id: str
    state_at: datetime
    known_at: datetime
    perspective: GovernancePerspective
    question_set_version: str
    source_registry_version: str
    query_pack_version: str
    extractor_versions: tuple[str, ...]
    reconstruction_version: str
    evidence_manifest_id: str
    evidence_manifest_hash: str
    actual_manifest_hash: str
    canonical_records: tuple[GovernanceRecord, ...]
    claims: tuple[GovernanceClaim, ...]
    evidence_spans: tuple[GovernanceEvidenceSpan, ...]
    question_level_coverage: tuple[GovernanceQuestionCoverageLink, ...]
    anchors: tuple[SnapshotAnchorInput, ...] = ()
    deltas: tuple[SnapshotDeltaInput, ...] = ()
    active_gap_ids: tuple[str, ...] = ()
    active_conflict_ids: tuple[str, ...] = ()
    pending_candidate_ids: tuple[str, ...] = ()
    completeness_status: CompletenessStatus = CompletenessStatus.INCOMPLETE
    state_time_precision: TimePrecision = TimePrecision.DATETIME
    known_time_precision: TimePrecision = TimePrecision.DATETIME
    original_timezone: str = "Asia/Shanghai"
    acquisition_scope: str = "governance_management"
    question_set_id: str = "governance_management_questions"
    supersedes_snapshot_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class SnapshotBuildResult:
    snapshot: GovernanceSnapshot
    idempotent_reuse: bool

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.governance_snapshot_id

    @property
    def snapshot_hash(self) -> str:
        return self.snapshot.canonical_snapshot_hash


def governance_snapshot_semantic_payload(
    request: SnapshotBuildRequest,
) -> dict[str, object]:
    """Return the complete, persisted preimage of a snapshot semantic hash."""

    return {
        "namespace": request.namespace,
        "company_id": request.company_id,
        "state_at": request.state_at,
        "known_at": request.known_at,
        "state_time_precision": request.state_time_precision,
        "known_time_precision": request.known_time_precision,
        "original_timezone": request.original_timezone,
        "perspective": request.perspective,
        "acquisition_scope": request.acquisition_scope,
        "question_set_id": request.question_set_id,
        "question_set_version": request.question_set_version,
        "source_registry_version": request.source_registry_version,
        "query_pack_version": request.query_pack_version,
        "extractor_versions": tuple(sorted(request.extractor_versions)),
        "reconstruction_version": request.reconstruction_version,
        "evidence_manifest_id": request.evidence_manifest_id,
        "evidence_manifest_hash": request.evidence_manifest_hash,
        "anchors": tuple(
            sorted(
                request.anchors,
                key=lambda item: (
                    item.question_id,
                    item.state_kind,
                    item.reference_at,
                    item.anchor_record_id,
                ),
            )
        ),
        "deltas": tuple(
            sorted(
                request.deltas,
                key=lambda item: (
                    item.effective_at,
                    item.announced_at or item.available_at,
                    item.delta_record_id,
                ),
            )
        ),
        "records": tuple(
            (item.record_id, item.canonical_hash)
            for item in sorted(
                request.canonical_records,
                key=lambda value: value.record_id,
            )
        ),
        "coverage": tuple(
            sorted(
                request.question_level_coverage,
                key=lambda item: item.question_id,
            )
        ),
        "active_gap_ids": tuple(sorted(request.active_gap_ids)),
        "active_conflict_ids": tuple(sorted(request.active_conflict_ids)),
        "pending_candidate_ids": tuple(sorted(request.pending_candidate_ids)),
        "completeness_status": request.completeness_status,
        "future_knowledge_used": (
            request.perspective == GovernancePerspective.RECONSTRUCTED
            and request.known_at > request.state_at
        ),
        "supersedes_snapshot_id": request.supersedes_snapshot_id,
    }


def canonical_governance_snapshot_hash(
    semantic_payload: Mapping[str, object],
) -> str:
    """Recompute a GovernanceSnapshot semantic hash from its frozen preimage."""

    return canonical_sha256(
        semantic_payload,
        schema_name="governance-snapshot-semantic",
        schema_version="1.0.0",
    )


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotIntegrityError("invalid_time", f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _expected_object_hash(value: object, hash_field: str) -> str:
    return value.calculate_canonical_hash(hash_field=hash_field)  # type: ignore[attr-defined]


def _validate_hash(value: object, hash_field: str, object_id: str) -> None:
    expected = _expected_object_hash(value, hash_field)
    actual = getattr(value, hash_field)
    if actual != expected:
        raise SnapshotIntegrityError(
            "hash_mismatch", f"{object_id} does not match its canonical payload"
        )


def _link_hash(value: object, hash_field: str = "canonical_hash") -> str:
    return _expected_object_hash(value, hash_field)


def _anchor_link(snapshot_id: str, item: SnapshotAnchorInput) -> SnapshotAnchorLink:
    identity = {
        "snapshot_id": snapshot_id,
        "question_id": item.question_id,
        "state_kind": item.state_kind,
        "anchor_record_id": item.anchor_record_id,
        "reference_at": item.reference_at,
        "available_at": item.available_at,
    }
    digest = canonical_sha256(
        identity, schema_name="governance-snapshot-anchor-link-id", schema_version="1"
    )
    provisional = SnapshotAnchorLink(
        anchor_link_id=f"govanchor:{digest}",
        governance_snapshot_id=snapshot_id,
        question_id=item.question_id,
        state_kind=item.state_kind,
        anchor_record_id=item.anchor_record_id,
        reference_at=item.reference_at,
        available_at=item.available_at,
        canonical_hash="0" * 64,
    )
    return provisional.model_copy(update={"canonical_hash": _link_hash(provisional)})


def _delta_link(
    snapshot_id: str, item: SnapshotDeltaInput, sequence: int
) -> SnapshotDeltaLink:
    identity = {
        "snapshot_id": snapshot_id,
        "question_id": item.question_id,
        "delta_record_id": item.delta_record_id,
        "disposition": item.disposition,
        "sequence": sequence,
        "effective_at": item.effective_at,
        "available_at": item.available_at,
        "exclusion_reason": item.exclusion_reason,
    }
    digest = canonical_sha256(
        identity, schema_name="governance-snapshot-delta-link-id", schema_version="1"
    )
    provisional = SnapshotDeltaLink(
        delta_link_id=f"govdelta:{digest}",
        governance_snapshot_id=snapshot_id,
        question_id=item.question_id,
        delta_record_id=item.delta_record_id,
        disposition=item.disposition,
        sequence=sequence,
        effective_at=item.effective_at,
        available_at=item.available_at,
        exclusion_reason=item.exclusion_reason,
        canonical_hash="0" * 64,
    )
    return provisional.model_copy(update={"canonical_hash": _link_hash(provisional)})


def _record_link(snapshot_id: str, record: GovernanceRecord) -> SnapshotRecordLink:
    identity = {
        "snapshot_id": snapshot_id,
        "record_id": record.record_id,
        "record_kind": record.kind,
        "record_hash": record.canonical_hash,
    }
    digest = canonical_sha256(
        identity, schema_name="governance-snapshot-record-link-id", schema_version="1"
    )
    provisional = SnapshotRecordLink(
        record_link_id=f"govrecordlink:{digest}",
        governance_snapshot_id=snapshot_id,
        record_id=record.record_id,
        record_kind=record.kind,
        role=SnapshotRecordRole.CANONICAL,
        canonical_hash="0" * 64,
    )
    return provisional.model_copy(update={"canonical_hash": _link_hash(provisional)})


def _semantic_item_value(item: object, field_name: str) -> object:
    if isinstance(item, Mapping):
        try:
            return item[field_name]
        except KeyError as exc:
            raise SnapshotIntegrityError(
                "semantic_payload_invalid",
                f"snapshot semantic item lacks {field_name}",
            ) from exc
    try:
        return getattr(item, field_name)
    except AttributeError as exc:
        raise SnapshotIntegrityError(
            "semantic_payload_invalid",
            f"snapshot semantic item lacks {field_name}",
        ) from exc


def _anchor_input_from_semantic(item: object) -> SnapshotAnchorInput:
    return SnapshotAnchorInput(
        question_id=str(_semantic_item_value(item, "question_id")),
        state_kind=str(_semantic_item_value(item, "state_kind")),
        anchor_record_id=str(_semantic_item_value(item, "anchor_record_id")),
        reference_at=_semantic_item_value(item, "reference_at"),  # type: ignore[arg-type]
        available_at=_semantic_item_value(item, "available_at"),  # type: ignore[arg-type]
    )


def _delta_input_from_semantic(item: object) -> SnapshotDeltaInput:
    disposition = _semantic_item_value(item, "disposition")
    if not isinstance(disposition, DeltaDisposition):
        try:
            disposition = DeltaDisposition(str(disposition))
        except ValueError as exc:
            raise SnapshotIntegrityError(
                "semantic_payload_invalid", "invalid delta disposition"
            ) from exc
    announced_at = (
        item.get("announced_at")
        if isinstance(item, Mapping)
        else getattr(item, "announced_at", None)
    )
    exclusion_reason = (
        item.get("exclusion_reason")
        if isinstance(item, Mapping)
        else getattr(item, "exclusion_reason", None)
    )
    return SnapshotDeltaInput(
        question_id=str(_semantic_item_value(item, "question_id")),
        delta_record_id=str(_semantic_item_value(item, "delta_record_id")),
        disposition=disposition,
        effective_at=_semantic_item_value(item, "effective_at"),  # type: ignore[arg-type]
        available_at=_semantic_item_value(item, "available_at"),  # type: ignore[arg-type]
        announced_at=announced_at,  # type: ignore[arg-type]
        exclusion_reason=(None if exclusion_reason is None else str(exclusion_reason)),
    )


def validate_governance_snapshot_semantic_hash(
    snapshot: GovernanceSnapshot,
    semantic_payload: Mapping[str, object],
) -> None:
    """Validate snapshot schema, semantic preimage, ID, links, and hash.

    The semantic preimage is required because the materialized snapshot does
    not duplicate the source namespace, announcement timestamps, or canonical
    record hashes. Renderers must obtain it from the same authoritative store
    as the snapshot instead of attempting a weaker projection hash.
    """

    try:
        GovernanceSnapshot.model_validate(snapshot.model_dump(mode="python"), strict=True)
    except ValidationError as exc:
        raise SnapshotIntegrityError(
            "schema_invalid", "materialized snapshot does not satisfy its schema"
        ) from exc
    expected_hash = canonical_governance_snapshot_hash(semantic_payload)
    if (
        snapshot.canonical_snapshot_hash != expected_hash
        or snapshot.governance_snapshot_id != f"govsnapshot:{expected_hash}"
    ):
        raise SnapshotIntegrityError(
            "snapshot_hash_mismatch",
            "snapshot ID/hash do not match the authoritative semantic preimage",
        )

    semantic_fields = (
        "company_id",
        "state_at",
        "known_at",
        "state_time_precision",
        "known_time_precision",
        "original_timezone",
        "perspective",
        "acquisition_scope",
        "question_set_id",
        "question_set_version",
        "source_registry_version",
        "query_pack_version",
        "extractor_versions",
        "reconstruction_version",
        "evidence_manifest_id",
        "evidence_manifest_hash",
        "active_gap_ids",
        "active_conflict_ids",
        "pending_candidate_ids",
        "completeness_status",
        "future_knowledge_used",
        "supersedes_snapshot_id",
    )
    for field_name in semantic_fields:
        if field_name not in semantic_payload:
            raise SnapshotIntegrityError(
                "semantic_payload_invalid",
                f"snapshot semantic preimage lacks {field_name}",
            )
        expected = semantic_payload[field_name]
        actual = getattr(snapshot, field_name)
        if canonical_json_text(expected) != canonical_json_text(actual):
            raise SnapshotIntegrityError(
                "snapshot_semantic_mismatch",
                f"snapshot field {field_name} differs from its semantic preimage",
            )

    expected_coverage = semantic_payload.get("coverage")
    if expected_coverage is None or canonical_json_text(expected_coverage) != canonical_json_text(
        snapshot.question_level_coverage
    ):
        raise SnapshotIntegrityError(
            "snapshot_semantic_mismatch",
            "snapshot coverage differs from its semantic preimage",
        )

    anchors_value = semantic_payload.get("anchors")
    deltas_value = semantic_payload.get("deltas")
    records_value = semantic_payload.get("records")
    if not isinstance(anchors_value, (tuple, list)) or not isinstance(
        deltas_value, (tuple, list)
    ) or not isinstance(records_value, (tuple, list)):
        raise SnapshotIntegrityError(
            "semantic_payload_invalid",
            "snapshot semantic preimage has invalid anchor/delta/record indexes",
        )
    expected_anchors = tuple(_anchor_input_from_semantic(item) for item in anchors_value)
    expected_deltas = tuple(_delta_input_from_semantic(item) for item in deltas_value)
    if len(expected_anchors) != len(snapshot.anchor_links) or len(expected_deltas) != len(
        snapshot.delta_links
    ):
        raise SnapshotIntegrityError(
            "snapshot_semantic_mismatch", "snapshot link counts differ from the preimage"
        )
    for item, link in zip(expected_anchors, snapshot.anchor_links, strict=True):
        if _anchor_link(snapshot.governance_snapshot_id, item) != link:
            raise SnapshotIntegrityError(
                "snapshot_semantic_mismatch", "snapshot anchor differs from the preimage"
            )
    for sequence, (item, link) in enumerate(
        zip(expected_deltas, snapshot.delta_links, strict=True), start=1
    ):
        if _delta_link(snapshot.governance_snapshot_id, item, sequence) != link:
            raise SnapshotIntegrityError(
                "snapshot_semantic_mismatch", "snapshot delta differs from the preimage"
            )

    record_hashes: dict[str, str] = {}
    for item in records_value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise SnapshotIntegrityError(
                "semantic_payload_invalid", "invalid canonical record hash entry"
            )
        record_id, record_hash = str(item[0]), str(item[1])
        if record_id in record_hashes:
            raise SnapshotIntegrityError(
                "semantic_payload_invalid", "duplicate canonical record identity"
            )
        record_hashes[record_id] = record_hash
    if tuple(sorted(record_hashes)) != snapshot.canonical_record_ids:
        raise SnapshotIntegrityError(
            "snapshot_semantic_mismatch",
            "snapshot canonical record index differs from the preimage",
        )
    if len(snapshot.record_links) != len(record_hashes):
        raise SnapshotIntegrityError(
            "snapshot_semantic_mismatch", "snapshot record links differ from the preimage"
        )
    for link in snapshot.record_links:
        record_hash = record_hashes.get(link.record_id)
        if record_hash is None:
            raise SnapshotIntegrityError(
                "snapshot_semantic_mismatch", "record link is absent from the preimage"
            )
        identity = {
            "snapshot_id": snapshot.governance_snapshot_id,
            "record_id": link.record_id,
            "record_kind": link.record_kind,
            "record_hash": record_hash,
        }
        digest = canonical_sha256(
            identity,
            schema_name="governance-snapshot-record-link-id",
            schema_version="1",
        )
        if (
            link.record_link_id != f"govrecordlink:{digest}"
            or link.canonical_hash != link.calculate_canonical_hash()
        ):
            raise SnapshotIntegrityError(
                "snapshot_semantic_mismatch",
                "record link ID/hash differs from the semantic record index",
            )


class GovernanceSnapshotService:
    """Namespace-bound, immutable in-memory snapshot publisher.

    The service deliberately accepts only opaque shared-manifest identity/hash;
    it does not copy the acquisition control plane.
    """

    def __init__(self, namespace: str) -> None:
        if not namespace.strip():
            raise ValueError("snapshot service namespace must be non-empty")
        self._namespace = namespace
        self._snapshots: dict[str, GovernanceSnapshot] = {}
        self._semantic_index: dict[str, str] = {}
        self._snapshot_namespaces: dict[str, str] = {}

    @property
    def namespace(self) -> str:
        return self._namespace

    def get_snapshot(self, snapshot_id: str) -> GovernanceSnapshot:
        try:
            return self._snapshots[snapshot_id]
        except KeyError as exc:
            raise SnapshotIntegrityError("snapshot_not_found", snapshot_id) from exc

    def list_snapshots(self) -> tuple[GovernanceSnapshot, ...]:
        return tuple(self._snapshots[item] for item in sorted(self._snapshots))

    def create_snapshot(self, request: SnapshotBuildRequest) -> SnapshotBuildResult:
        return self.create(request)

    def build_snapshot(self, request: SnapshotBuildRequest) -> SnapshotBuildResult:
        return self.create(request)

    def build(self, request: SnapshotBuildRequest) -> SnapshotBuildResult:
        return self.create(request)

    def create(self, request: SnapshotBuildRequest) -> SnapshotBuildResult:
        self._validate_request(request)
        semantic_payload = governance_snapshot_semantic_payload(request)
        semantic_hash = canonical_governance_snapshot_hash(semantic_payload)
        existing_id = self._semantic_index.get(semantic_hash)
        if existing_id is not None:
            return SnapshotBuildResult(
                snapshot=self._snapshots[existing_id], idempotent_reuse=True
            )

        snapshot_id = f"govsnapshot:{semantic_hash}"
        if snapshot_id in self._snapshots:
            raise SnapshotIntegrityError(
                "immutable_conflict", "snapshot ID is already bound to another payload"
            )
        try:
            anchors = tuple(
                sorted(
                    (_anchor_link(snapshot_id, item) for item in request.anchors),
                    key=lambda item: item.anchor_link_id,
                )
            )
        except ValidationError as exc:
            raise SnapshotIntegrityError("schema_invalid", str(exc)) from exc
        ordered_deltas = sorted(
            request.deltas,
            key=lambda item: (
                item.effective_at,
                item.announced_at or item.available_at,
                item.delta_record_id,
            ),
        )
        try:
            delta_links = tuple(
                _delta_link(snapshot_id, item, sequence)
                for sequence, item in enumerate(ordered_deltas, start=1)
            )
        except ValidationError as exc:
            raise SnapshotIntegrityError("schema_invalid", str(exc)) from exc
        records = tuple(sorted(request.canonical_records, key=lambda item: item.record_id))
        try:
            record_links = tuple(
                sorted(
                    (_record_link(snapshot_id, item) for item in records),
                    key=lambda item: item.record_link_id,
                )
            )
        except ValidationError as exc:
            raise SnapshotIntegrityError("schema_invalid", str(exc)) from exc
        created_at = _aware_utc(
            request.created_at or datetime.now(timezone.utc), field_name="created_at"
        )
        try:
            snapshot = GovernanceSnapshot(
                governance_snapshot_id=snapshot_id,
                company_id=request.company_id,
                state_at=request.state_at,
                known_at=request.known_at,
                state_time_precision=request.state_time_precision,
                known_time_precision=request.known_time_precision,
                original_timezone=request.original_timezone,
                perspective=request.perspective,
                acquisition_scope=request.acquisition_scope,
                question_set_id=request.question_set_id,
                question_set_version=request.question_set_version,
                source_registry_version=request.source_registry_version,
                query_pack_version=request.query_pack_version,
                extractor_versions=tuple(sorted(request.extractor_versions)),
                reconstruction_version=request.reconstruction_version,
                evidence_manifest_id=request.evidence_manifest_id,
                evidence_manifest_hash=request.evidence_manifest_hash,
                anchor_links=anchors,
                delta_links=delta_links,
                record_links=record_links,
                canonical_record_ids=tuple(item.record_id for item in records),
                active_gap_ids=tuple(sorted(request.active_gap_ids)),
                active_conflict_ids=tuple(sorted(request.active_conflict_ids)),
                pending_candidate_ids=tuple(sorted(request.pending_candidate_ids)),
                question_level_coverage=tuple(
                    sorted(
                        request.question_level_coverage,
                        key=lambda item: item.question_id,
                    )
                ),
                completeness_status=request.completeness_status,
                future_knowledge_used=(
                    request.perspective == GovernancePerspective.RECONSTRUCTED
                    and request.known_at > request.state_at
                ),
                supersedes_snapshot_id=request.supersedes_snapshot_id,
                canonical_snapshot_hash=semantic_hash,
                created_at=created_at,
            )
        except ValidationError as exc:
            raise SnapshotIntegrityError("schema_invalid", str(exc)) from exc
        validate_governance_snapshot_semantic_hash(snapshot, semantic_payload)
        self._snapshots[snapshot_id] = snapshot
        self._semantic_index[semantic_hash] = snapshot_id
        self._snapshot_namespaces[snapshot_id] = request.namespace
        return SnapshotBuildResult(snapshot=snapshot, idempotent_reuse=False)

    def _validate_request(self, request: SnapshotBuildRequest) -> None:
        if request.namespace != self._namespace:
            raise SnapshotIntegrityError(
                "cross_namespace", "request namespace differs from service binding"
            )
        if request.acquisition_scope != "governance_management":
            raise SnapshotIntegrityError("cross_scope", "unsupported acquisition scope")
        if request.question_set_id != "governance_management_questions":
            raise SnapshotIntegrityError("cross_scope", "unsupported question set")
        if not isinstance(request.perspective, GovernancePerspective):
            raise SnapshotIntegrityError("invalid_time", "perspective is not a closed enum")
        if len(request.evidence_manifest_hash) != 64 or any(
            char not in "0123456789abcdef" for char in request.evidence_manifest_hash
        ):
            raise SnapshotIntegrityError(
                "manifest_hash_mismatch", "manifest hash is not canonical SHA-256"
            )
        state_at = _aware_utc(request.state_at, field_name="state_at")
        known_at = _aware_utc(request.known_at, field_name="known_at")
        if known_at < state_at:
            raise SnapshotIntegrityError("invalid_time", "known_at precedes state_at")
        if request.perspective == GovernancePerspective.STRICT and known_at != state_at:
            raise SnapshotIntegrityError(
                "invalid_time", "strict perspective requires equal query instants"
            )
        if request.actual_manifest_hash != request.evidence_manifest_hash:
            raise SnapshotIntegrityError(
                "manifest_hash_mismatch", "manifest bytes do not match the fixed hash"
            )
        if not request.extractor_versions:
            raise SnapshotIntegrityError(
                "cross_scope", "snapshot must freeze at least one extractor version"
            )
        if len(request.extractor_versions) != len(set(request.extractor_versions)):
            raise SnapshotIntegrityError(
                "cross_scope", "extractor versions must be unique"
            )
        if request.completeness_status == CompletenessStatus.COMPLETE and (
            request.active_gap_ids or request.active_conflict_ids
        ):
            raise SnapshotIntegrityError(
                "invalid_completeness",
                "complete snapshot cannot carry active gaps or conflicts",
            )
        if (
            request.completeness_status == CompletenessStatus.CONFLICTED
            and not request.active_conflict_ids
        ):
            raise SnapshotIntegrityError(
                "invalid_completeness", "conflicted snapshot requires a conflict"
            )
        for name, values in (
            ("active_gap_ids", request.active_gap_ids),
            ("active_conflict_ids", request.active_conflict_ids),
            ("pending_candidate_ids", request.pending_candidate_ids),
        ):
            if len(values) != len(set(values)):
                raise SnapshotIntegrityError(
                    "broken_lineage", f"{name} contains duplicate identities"
                )

        claim_by_id: dict[str, GovernanceClaim] = {}
        for claim in request.claims:
            if claim.claim_id in claim_by_id:
                raise SnapshotIntegrityError(
                    "broken_lineage", "claim identities must be unique"
                )
            claim_by_id[claim.claim_id] = claim
            if claim.company_id != request.company_id:
                raise SnapshotIntegrityError(
                    "cross_scope", "claim crosses the requested company namespace"
                )
            if claim.evidence_manifest_id != request.evidence_manifest_id:
                raise SnapshotIntegrityError(
                    "broken_lineage", "claim references a different evidence manifest"
                )
            if claim.available_at > known_at:
                raise SnapshotIntegrityError(
                    "future_leak", "claim is not available at the query known_at"
                )
            _validate_hash(claim, "canonical_hash", claim.claim_id)

        span_by_id: dict[str, GovernanceEvidenceSpan] = {}
        for span in request.evidence_spans:
            if span.evidence_span_id in span_by_id:
                raise SnapshotIntegrityError(
                    "broken_lineage", "evidence span identities must be unique"
                )
            span_by_id[span.evidence_span_id] = span

        record_by_id: dict[str, GovernanceRecord] = {}
        for record in request.canonical_records:
            if isinstance(record, OwnershipPosition):
                raise SnapshotIntegrityError(
                    "cross_scope",
                    "OwnershipPosition is embedded and cannot be linked as a top-level record",
                )
            if record.record_id in record_by_id:
                raise SnapshotIntegrityError(
                    "hash_mismatch", "record identities must be unique"
                )
            record_by_id[record.record_id] = record
            if record.company_id != request.company_id:
                raise SnapshotIntegrityError(
                    "cross_scope", "record crosses the requested company namespace"
                )
            if record.available_at > known_at:
                raise SnapshotIntegrityError(
                    "future_leak", "record is not available at the query known_at"
                )
            _validate_hash(record, "canonical_hash", record.record_id)
            if isinstance(record, OwnershipSnapshot):
                for position in record.positions:
                    _validate_hash(position, "canonical_hash", position.position_id)
            missing_claims = set(record.claim_ids) - set(claim_by_id)
            missing_spans = set(record.evidence_span_ids) - set(span_by_id)
            if missing_claims or missing_spans:
                raise SnapshotIntegrityError(
                    "broken_lineage",
                    f"record {record.record_id} has unresolved claim/span lineage",
                )
            for claim_id in record.claim_ids:
                claim = claim_by_id[claim_id]
                if claim.question_id != record.question_id:
                    raise SnapshotIntegrityError(
                        "cross_scope", "record and claim question identities disagree"
                    )
                if claim.evidence_span_id not in record.evidence_span_ids:
                    raise SnapshotIntegrityError(
                        "broken_lineage",
                        "record omits the evidence span used by one of its claims",
                    )
                span = span_by_id.get(claim.evidence_span_id)
                if span is None:
                    raise SnapshotIntegrityError(
                        "broken_lineage", "claim references an unknown evidence span"
                    )
                if (
                    span.raw_snapshot_id != claim.raw_snapshot_id
                    or span.content_hash != claim.content_hash
                ):
                    raise SnapshotIntegrityError(
                        "broken_lineage", "claim and evidence span provenance disagree"
                    )

        coverage_questions = {item.question_id for item in request.question_level_coverage}
        if len(coverage_questions) != len(request.question_level_coverage):
            raise SnapshotIntegrityError(
                "cross_scope", "question-level coverage contains duplicate questions"
            )
        if not coverage_questions:
            raise SnapshotIntegrityError(
                "cross_scope", "snapshot requires question-level coverage"
            )
        for record in request.canonical_records:
            if record.question_id not in coverage_questions:
                raise SnapshotIntegrityError(
                    "cross_scope", "record question is absent from snapshot coverage"
                )

        anchor_keys = [
            (item.question_id, item.state_kind, item.anchor_record_id)
            for item in request.anchors
        ]
        if len(anchor_keys) != len(set(anchor_keys)):
            raise SnapshotIntegrityError(
                "broken_lineage", "snapshot anchors must be unique"
            )
        for anchor in request.anchors:
            available_at = _aware_utc(
                anchor.available_at, field_name="anchor.available_at"
            )
            if available_at > known_at:
                raise SnapshotIntegrityError(
                    "future_leak", "anchor is not visible at known_at"
                )
            _aware_utc(anchor.reference_at, field_name="anchor.reference_at")
            if anchor.anchor_record_id not in record_by_id:
                raise SnapshotIntegrityError(
                    "broken_lineage", "anchor references an unknown typed record"
                )
            if record_by_id[anchor.anchor_record_id].question_id != anchor.question_id:
                raise SnapshotIntegrityError(
                    "cross_scope", "anchor question differs from its typed record"
                )
            anchor_record = record_by_id[anchor.anchor_record_id]
            if anchor_record.reference_at != anchor.reference_at:
                raise SnapshotIntegrityError(
                    "broken_lineage", "anchor link reference time differs from its record"
                )
            if anchor_record.available_at != anchor.available_at:
                raise SnapshotIntegrityError(
                    "broken_lineage", "anchor link availability differs from its record"
                )
            if anchor.reference_at > state_at:
                raise SnapshotIntegrityError(
                    "invalid_time", "anchor reference_at is after state_at"
                )
        delta_ids = [item.delta_record_id for item in request.deltas]
        if len(delta_ids) != len(set(delta_ids)):
            raise SnapshotIntegrityError(
                "broken_lineage", "snapshot deltas must be unique"
            )
        for delta in request.deltas:
            available_at = _aware_utc(
                delta.available_at, field_name="delta.available_at"
            )
            if available_at > known_at:
                raise SnapshotIntegrityError(
                    "future_leak", "delta is not visible at known_at"
                )
            _aware_utc(delta.effective_at, field_name="delta.effective_at")
            if delta.announced_at is not None:
                _aware_utc(delta.announced_at, field_name="delta.announced_at")
            if delta.delta_record_id not in record_by_id:
                raise SnapshotIntegrityError(
                    "broken_lineage", "delta references an unknown typed record"
                )
            if record_by_id[delta.delta_record_id].question_id != delta.question_id:
                raise SnapshotIntegrityError(
                    "cross_scope", "delta question differs from its typed record"
                )
            delta_record = record_by_id[delta.delta_record_id]
            if delta_record.effective_at != delta.effective_at:
                raise SnapshotIntegrityError(
                    "broken_lineage", "delta link effective time differs from its record"
                )
            if delta_record.available_at != delta.available_at:
                raise SnapshotIntegrityError(
                    "broken_lineage", "delta link availability differs from its record"
                )
            if delta.disposition == DeltaDisposition.APPLIED and delta.effective_at > state_at:
                raise SnapshotIntegrityError(
                    "invalid_time", "applied delta is effective after state_at"
                )
            if (
                delta.disposition == DeltaDisposition.APPLIED
                and delta.exclusion_reason is not None
            ) or (
                delta.disposition == DeltaDisposition.EXCLUDED
                and not delta.exclusion_reason
            ):
                raise SnapshotIntegrityError(
                    "cross_scope", "delta disposition and exclusion reason disagree"
                )
        if request.supersedes_snapshot_id is not None:
            old = self._snapshots.get(request.supersedes_snapshot_id)
            if old is None:
                raise SnapshotIntegrityError(
                    "snapshot_not_found", "superseded snapshot does not exist"
                )
            if (
                old.company_id != request.company_id
                or self._snapshot_namespaces.get(old.governance_snapshot_id)
                != request.namespace
            ):
                raise SnapshotIntegrityError(
                    "cross_namespace", "supersedes relation crosses company/namespace"
                )

    @staticmethod
    def _semantic_payload(request: SnapshotBuildRequest) -> dict[str, object]:
        return governance_snapshot_semantic_payload(request)


__all__ = [
    "GovernanceSnapshotService",
    "SnapshotAnchorInput",
    "SnapshotBuildRequest",
    "SnapshotBuildResult",
    "SnapshotDeltaInput",
    "SnapshotIntegrityError",
    "canonical_governance_snapshot_hash",
    "governance_snapshot_semantic_payload",
    "validate_governance_snapshot_semantic_hash",
]
