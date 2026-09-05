from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    AcquisitionOutcome,
    AcquisitionRun,
    BarrierResolution,
    CheckpointPartition,
    CheckpointPosition,
    CoverageEntry,
    CoveragePlanDisposition,
    CoverageResolution,
    CoverageResolutionStatus,
    PhysicalQueryPlanItem,
    SourceCheckpoint,
    ValidatorAnchor,
    stable_acquisition_id,
)
from .repository import AcquisitionRepository


BLOCKING_OUTCOMES = frozenset(
    {
        AcquisitionOutcome.RESTRICTED,
        AcquisitionOutcome.PAYWALLED,
        AcquisitionOutcome.LOGIN_REQUIRED,
        AcquisitionOutcome.RATE_LIMITED,
        AcquisitionOutcome.TIMEOUT,
        AcquisitionOutcome.NETWORK_FAILED,
        AcquisitionOutcome.PARSE_FAILED,
        AcquisitionOutcome.POLICY_SKIPPED,
        AcquisitionOutcome.PARTIAL_SUCCESS,
    }
)
COMPLETING_OUTCOMES = frozenset(
    {
        AcquisitionOutcome.SUCCESS,
        AcquisitionOutcome.UNCHANGED,
        AcquisitionOutcome.NO_DATA,
    }
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("checkpoint 时间必须包含时区")
    return value.astimezone(timezone.utc)


def is_blocking_outcome(value: AcquisitionOutcome | str) -> bool:
    return AcquisitionOutcome(value) in BLOCKING_OUTCOMES


def is_completing_outcome(value: AcquisitionOutcome | str) -> bool:
    return AcquisitionOutcome(value) in COMPLETING_OUTCOMES


def incremental_window(
    checkpoint: SourceCheckpoint,
    *,
    overlap_days: int,
    as_of: datetime,
) -> tuple[datetime, datetime]:
    """Return the immutable overlap window for one compatible checkpoint.

    A checkpoint with no safe source lower-bound is deliberately unusable for
    incremental planning.  Callers must request baseline or reconcile instead.
    """

    if overlap_days < 0:
        raise ValueError("overlap_days不得为负数")
    if checkpoint.source_safe_through is None:
        raise ValueError("checkpoint没有安全来源水位线，不能执行incremental")
    end = _utc(as_of)
    start = checkpoint.source_safe_through.time_upper_bound - timedelta(
        days=overlap_days
    )
    if end <= start:
        raise ValueError("incremental as_of必须晚于重叠回看起点")
    return start, end


def source_safe_lower_bound(
    partitions: Sequence[CheckpointPartition],
) -> CheckpointPosition | None:
    """Compute the conservative lower-bound across every required partition."""

    if not partitions or any(item.safe_through is None for item in partitions):
        return None
    positions = [item.safe_through for item in partitions]
    assert all(item is not None for item in positions)
    return min(positions, key=lambda item: item.ordering_key)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BarrierDraft:
    """An opening barrier waiting for the checkpoint id allocated at finalize."""

    barrier_id: str
    source_definition_id: str
    source_definition_version: str
    partition_key: str
    work_position: str
    retry_group_id: str
    opening_attempt_id: str
    query_semantics_hash: str | None = None
    canonical_resource_id: str | None = None
    reason_code: str | None = None
    outcome: AcquisitionOutcome | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def bind(self, checkpoint_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "barrier_id": self.barrier_id,
            "checkpoint_id": checkpoint_id,
            "source_definition_id": self.source_definition_id,
            "source_definition_version": self.source_definition_version,
            "partition_key": self.partition_key,
            "work_position": self.work_position,
            "canonical_resource_id": self.canonical_resource_id,
            "retry_group_id": self.retry_group_id,
            "opening_attempt_id": self.opening_attempt_id,
            "created_at": _utc(self.created_at).isoformat(),
            "reason_code": self.reason_code,
            "outcome": None if self.outcome is None else self.outcome.value,
        }
        if self.query_semantics_hash is not None:
            payload["query_semantics_hash"] = self.query_semantics_hash
        return payload


def barrier_for_attempt(
    *,
    attempt: Any,
    plan_item: PhysicalQueryPlanItem,
    work_position: str,
    outcome: AcquisitionOutcome | str,
    reason_code: str,
    canonical_resource_id: str | None = None,
    created_at: datetime | None = None,
) -> BarrierDraft:
    selected = AcquisitionOutcome(outcome)
    if not is_blocking_outcome(selected):
        raise ValueError("success/unchanged/no_data不得打开checkpoint barrier")
    identity = {
        "source_definition_id": plan_item.source_definition_id,
        "source_definition_version": plan_item.source_definition_version,
        "partition_key": plan_item.partition_key,
        "work_position": work_position,
        "canonical_resource_id": canonical_resource_id,
        "retry_group_id": attempt.retry_group_id,
        "opening_attempt_id": attempt.attempt_id,
    }
    return BarrierDraft(
        barrier_id=stable_acquisition_id("barrier", identity),
        source_definition_id=plan_item.source_definition_id,
        source_definition_version=plan_item.source_definition_version,
        partition_key=plan_item.partition_key,
        work_position=work_position,
        canonical_resource_id=canonical_resource_id,
        retry_group_id=attempt.retry_group_id,
        opening_attempt_id=attempt.attempt_id,
        reason_code=reason_code,
        outcome=selected,
        created_at=created_at or datetime.now(timezone.utc),
    )


@dataclass(frozen=True, slots=True)
class PlanProgress:
    plan_item: PhysicalQueryPlanItem
    complete: bool
    last_canonical_resource_id: str | None = None
    validator_anchors: tuple[ValidatorAnchor, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckpointBuild:
    checkpoint: SourceCheckpoint
    expected_parent_version: int | None
    opening_barriers: tuple[dict[str, Any], ...]


class CheckpointEngine:
    """Pure conservative checkpoint calculation used by every run kind.

    The engine never treats a static planning disposition as executable work,
    and never advances through an incomplete physical plan item.  Existing
    safe positions are retained during overlap runs; unresolved barriers are
    carried forward until an explicit immutable resolution is supplied.
    """

    def build(
        self,
        *,
        run: AcquisitionRun,
        source_definition_id: str,
        source_definition_version: str,
        overlap_days: int,
        progress: Sequence[PlanProgress],
        opening_barriers: Sequence[BarrierDraft] = (),
        resolved_barrier_ids: Iterable[str] = (),
        previous: SourceCheckpoint | None = None,
        lease_epoch: int = 1,
        created_at: datetime | None = None,
    ) -> CheckpointBuild:
        if run.run_kind.value != "production":
            raise ValueError("smoke/ad_hoc run不得推进production checkpoint")
        if overlap_days < 0:
            raise ValueError("overlap_days不得为负数")
        source_progress = [
            item
            for item in progress
            if item.plan_item.source_definition_id == source_definition_id
            and str(item.plan_item.source_definition_version)
            == str(source_definition_version)
        ]
        grouped: dict[str, list[PlanProgress]] = {}
        for item in source_progress:
            grouped.setdefault(item.plan_item.partition_key, []).append(item)
        previous_partitions = {
            item.partition_key: item for item in (() if previous is None else previous.partitions)
        }
        partitions: list[CheckpointPartition] = []
        for partition_key in sorted(set(grouped) | set(previous_partitions)):
            items = sorted(
                grouped.get(partition_key, ()),
                key=lambda item: (
                    item.plan_item.time_start,
                    item.plan_item.time_end,
                    item.plan_item.execution_key,
                ),
            )
            old = previous_partitions.get(partition_key)
            safe = None if old is None else old.safe_through
            validators: dict[tuple[str, str], ValidatorAnchor] = {}
            if old is not None:
                validators.update(
                    {
                        (anchor.canonical_resource_id, anchor.resource_url): anchor
                        for anchor in old.validator_anchors
                    }
                )
            blocked_start = min(
                (item.plan_item.time_start for item in items if not item.complete),
                default=None,
            )
            for item in items:
                for anchor in item.validator_anchors:
                    key = (anchor.canonical_resource_id, anchor.resource_url)
                    known = validators.get(key)
                    if known is None or (anchor.observed_at, anchor.snapshot_id) > (known.observed_at, known.snapshot_id):
                        validators[key] = anchor
                if not item.complete:
                    continue
                if safe is not None and item.plan_item.time_start > safe.time_upper_bound:
                    break
                # A longer historical interval must not hide a newly failed
                # overlapping recheck. Retain the old safe position, but do
                # not advance through the unresolved interval.
                if blocked_start is not None and item.plan_item.time_end > blocked_start:
                    continue
                candidate = CheckpointPosition(
                    time_upper_bound=item.plan_item.time_end,
                    canonical_resource_id=(
                        item.last_canonical_resource_id
                        or f"__slice_end__:{item.plan_item.plan_item_id}"
                    ),
                )
                if safe is None or candidate.ordering_key > safe.ordering_key:
                    safe = candidate
            execution_key = (
                items[0].plan_item.execution_key
                if items
                else (old.execution_key if old is not None else partition_key)
            )
            partitions.append(
                CheckpointPartition(
                    partition_key=partition_key,
                    execution_key=execution_key,
                    safe_through=safe,
                    opaque_cursor=None,
                    validator_anchors=tuple(
                        sorted(
                            validators.values(),
                            key=lambda item: (
                                item.canonical_resource_id,
                                item.resource_url,
                                item.observed_at,
                                item.snapshot_id,
                            ),
                        )
                    ),
                    unresolved_barrier_ids=(),
                )
            )

        resolved = set(resolved_barrier_ids)
        unresolved = set(
            () if previous is None else previous.unresolved_barrier_ids
        ) - resolved
        unresolved.update(
            item.barrier_id for item in opening_barriers if item.barrier_id not in resolved
        )
        by_partition: dict[str, list[str]] = {}
        for item in opening_barriers:
            if item.barrier_id in unresolved:
                by_partition.setdefault(item.partition_key, []).append(item.barrier_id)
        if previous is not None:
            for item in previous.partitions:
                for barrier_id in item.unresolved_barrier_ids:
                    if barrier_id in unresolved:
                        by_partition.setdefault(item.partition_key, []).append(barrier_id)
        partitions = [
            item.model_copy(
                update={
                    "unresolved_barrier_ids": tuple(
                        sorted(set(by_partition.get(item.partition_key, ())))
                    )
                }
            )
            for item in partitions
        ]
        safe_lower_bound = source_safe_lower_bound(partitions)
        checkpoint_version = 1 if previous is None else previous.checkpoint_version + 1
        checkpoint_identity = {
            "run_id": run.run_id,
            "source_definition_id": source_definition_id,
            "source_definition_version": source_definition_version,
            "checkpoint_version": checkpoint_version,
        }
        checkpoint = SourceCheckpoint(
            checkpoint_id=stable_acquisition_id("checkpoint", checkpoint_identity),
            ticker=run.ticker,
            source_definition_id=source_definition_id,
            source_definition_version=str(source_definition_version),
            question_set_version=run.question_set_version,
            checkpoint_version=checkpoint_version,
            parent_checkpoint_id=None if previous is None else previous.checkpoint_id,
            parent_checkpoint_version=(
                None if previous is None else previous.checkpoint_version
            ),
            source_safe_through=safe_lower_bound,
            partitions=tuple(partitions),
            overlap_days=overlap_days,
            latest_successful_run_id=(
                run.run_id
                if not unresolved and all(item.complete for item in source_progress)
                else (
                    None
                    if previous is None
                    else previous.latest_successful_run_id
                )
            ),
            unresolved_barrier_ids=tuple(sorted(unresolved)),
            created_at=_utc(created_at or datetime.now(timezone.utc)),
            finalized_lease_epoch=lease_epoch,
        )
        bound_barriers = tuple(
            item.bind(checkpoint.checkpoint_id) for item in opening_barriers
        )
        return CheckpointBuild(
            checkpoint=checkpoint,
            expected_parent_version=(
                None if previous is None else previous.checkpoint_version
            ),
            opening_barriers=bound_barriers,
        )


def static_coverage_resolution(entry: CoverageEntry) -> CoverageResolution:
    if entry.plan_disposition != CoveragePlanDisposition.STATIC_POLICY_SKIPPED:
        raise ValueError("required coverage不能创建静态policy resolution")
    return CoverageResolution(
        coverage_entry_id=entry.coverage_entry_id,
        status=CoverageResolutionStatus.STATIC_POLICY_SKIPPED,
        material_gap_count=0,
        reason_codes=(entry.static_reason_code or "policy_skipped",),
    )


class CheckpointRepository:
    """Focused facade for checkpoint CAS, barrier resolution, and atomic finalize."""

    def __init__(self, repository: AcquisitionRepository) -> None:
        self.repository = repository

    def latest(
        self,
        ticker: str,
        source_definition_id: str,
        source_definition_version: str | int,
        question_set_version: str,
    ) -> Any | None:
        return self.repository.latest_checkpoint(
            ticker,
            source_definition_id,
            source_definition_version,
            question_set_version,
        )

    def append(
        self,
        checkpoint: Any,
        *,
        expected_parent_version: int | None,
        run_id: str,
        owner_token: str,
        lease_epoch: int,
    ) -> None:
        self.repository.save_checkpoint(
            checkpoint,
            expected_parent_version=expected_parent_version,
            run_id=run_id,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )

    def open_barrier(self, barrier: Any) -> None:
        self.repository.open_barrier(barrier)

    def resolve_barrier(
        self,
        resolution: Any,
        *,
        owner_token: str,
        lease_epoch: int,
    ) -> None:
        self.repository.append_barrier_resolution(
            resolution,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )

    def finalize(
        self,
        run_id: str,
        *,
        run_event: Any,
        coverage_resolutions: Iterable[Any] = (),
        checkpoints: Iterable[tuple[Any, int | None]] = (),
        opening_barriers: Iterable[Any] = (),
        barrier_resolutions: Iterable[Any] = (),
        owner_token: str,
        lease_epoch: int,
    ) -> None:
        self.repository.finalize_run(
            run_id,
            run_event=run_event,
            coverage_resolutions=coverage_resolutions,
            checkpoints=checkpoints,
            opening_barriers=opening_barriers,
            barrier_resolutions=barrier_resolutions,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )


CheckpointStore = CheckpointRepository
