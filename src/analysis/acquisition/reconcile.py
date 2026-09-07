"""Read-only, authoritative selection of one finalized run's repair target."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .models import AcquisitionMode, AcquisitionRunEventType, AttemptKind, CoveragePlanDisposition
from .repository import AcquisitionNotFoundError
from .registry import canonical_json_sha256


RANGE_POLICY_VERSION = "bounded-parent-range-v1"


@dataclass(frozen=True, slots=True)
class ReconcileSelection:
    parent_run_id: str
    start_at: datetime
    target: Mapping[str, Any]


def _position_key(value: str) -> tuple:
    try:
        position = json.loads(value)
        return (position.get("kind", ""), int(position.get("page") or 0),
                position.get("cursor") or "", position.get("canonical_resource_id") or "")
    except (ValueError, TypeError, AttributeError):
        return ("", 0, value, "")


def _aware_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("reconcile父范围时间必须包含时区")
    return parsed


def _parent_range_floor(repository, parent, plans, selected, frozen_definitions):
    """Validate a frozen repair range against its persisted discovery graph."""
    try:
        target = parent.reconcile_target
        lower = _aware_time(target["effective_range"]["time_start"])
        upper = _aware_time(target["effective_range"]["time_end"])
        if not lower <= selected.time_start < selected.time_end <= upper <= parent.as_of:
            raise ValueError("目标不在父恢复范围内")
        policy_fields = {"range_policy_version", "range_floor", "range_floor_source"}
        if policy_fields.intersection(target):
            if target.get("range_policy_version") != RANGE_POLICY_VERSION:
                raise ValueError("未知或不完整range_policy_version")
            if _aware_time(target["range_floor"]) != lower:
                raise ValueError("range_floor与effective_range矛盾")
            ancestor = repository.get_run(parent.parent_run_id)
            expected_source = (f"parent_effective_range:{ancestor.run_id}"
                               if ancestor.mode == AcquisitionMode.RECONCILE
                               else "source_overlap_and_earliest_available_at")
            if target["range_floor_source"] != expected_source:
                raise ValueError("range_floor_source与父运行关系矛盾")
        if (target["parent_run_id"] != parent.parent_run_id or
                target["resolved_parent_run_id"] != parent.parent_run_id or
                _aware_time(target["start_at"]) != lower or
                _aware_time(target["as_of"]) != parent.as_of):
            raise ValueError("父target身份或时间矛盾")

        # The target names a plan in the preceding run, not in this parent.
        origin = next(p for p in repository.list_physical_query_plan_items(parent.parent_run_id)
                      if p.plan_item_id == target["plan_item_id"])
        if (origin.attempt_kind != AttemptKind.DISCOVERY or
                any(getattr(origin, field) != target[field] for field in (
                    "source_definition_id", "source_definition_version", "query_id", "partition_key")) or
                origin.time_start != _aware_time(target["parent_time_start"]) or
                origin.time_end != _aware_time(target["parent_time_end"]) or
                not lower <= origin.time_start < upper <= origin.time_end):
            raise ValueError("父target与原计划矛盾")
        definition = next(d for d in frozen_definitions
                          if d.source_definition_id == selected.source_definition_id
                          and str(d.version) == selected.source_definition_version)
        if definition.source_definition_id != target["source_definition_id"]:
            raise ValueError("父target来源不一致")
        queries = {q.query_id: q for q in definition.queries}
        allowed = set()

        def include(query_id):
            if query_id in allowed:
                return
            allowed.add(query_id)
            for binding in queries[query_id].parameter_bindings.values():
                include(binding.source_query_id)

        include(target["query_id"])
        intervals = {query_id: [] for query_id in allowed}
        for plan in plans.values():
            if plan.attempt_kind != AttemptKind.DISCOVERY:
                continue
            query = queries[plan.query_id]
            if (plan.query_id not in allowed or
                    plan.source_definition_id != definition.source_definition_id or
                    plan.source_definition_version != str(definition.version) or
                    plan.partition_key != query.partition_key or
                    plan.pagination_fingerprint != canonical_json_sha256(query.pagination) or
                    not lower <= plan.time_start < plan.time_end <= upper):
                raise ValueError("父discovery计划与冻结来源或范围矛盾")
            intervals[plan.query_id].append((plan.time_start, plan.time_end))
        # Reject a fabricated floor, missing prefix/suffix, gap or overlap.
        for query_id, slices in intervals.items():
            cursor = max(lower, queries[query_id].earliest_available_at or lower)
            for start, end in sorted(slices):
                if start != cursor:
                    raise ValueError("父discovery范围不连续")
                cursor = end
            if not slices or cursor != upper:
                raise ValueError("父discovery计划未覆盖冻结范围")
        return lower
    except (KeyError, TypeError, AttributeError, StopIteration, ValueError) as exc:
        raise ValueError(f"reconcile父恢复范围无效: {exc}") from exc


def select_reconcile_target(repository, definitions, parent_run_id, *, as_of, now=None,
                            frozen_definitions=()):
    parent = repository.get_run(parent_run_id)
    if not any(event.event_type == AcquisitionRunEventType.FINALIZED
               for event in repository.list_run_events(parent_run_id)):
        raise ValueError("reconcile父运行必须已有finalized终态；请创建新的运行取得可审计终态")
    try:
        lease = repository.get_lease(parent_run_id)
    except AcquisitionNotFoundError:
        lease = None
    if lease is not None and lease.is_active_at(now or datetime.now(timezone.utc)):
        raise ValueError("reconcile父运行仍有active lease")
    entries = {entry.coverage_entry_id: entry
               for entry in repository.list_coverage_entries(parent_run_id)}
    plans = {plan.plan_item_id: plan
             for plan in repository.list_physical_query_plan_items(parent_run_id)}
    links = repository.list_physical_query_coverage_links(run_id=parent_run_id)
    linked = {}
    for link in links:
        if link.plan_item_id not in plans or link.coverage_entry_id not in entries:
            raise ValueError("reconcile父运行coverage graph引用不完整")
        linked.setdefault(link.coverage_entry_id, []).append(plans[link.plan_item_id])
    required = {key: entry for key, entry in entries.items()
                if entry.plan_disposition == CoveragePlanDisposition.REQUIRED}
    if not required or any(key not in linked for key in required):
        raise ValueError("reconcile父运行没有可解释的required coverage graph")
    resolutions = {row.coverage_entry_id: row
                   for row in repository.list_coverage_resolutions(parent_run_id)}

    def root(plan):
        return plans[plan.parent_plan_item_id] if plan.parent_plan_item_id else plan

    def key(plan, entry, position="", identity=""):
        plan = root(plan)
        return (plan.time_start, plan.ordinal, _position_key(position),
                plan.source_definition_id, str(plan.source_definition_version),
                plan.partition_key, entry.coverage_entry_id, identity)

    all_parent_barriers = []
    unresolved_ids = {row["barrier_id"] for row in
                      repository.list_checkpoint_barriers(unresolved_only=True)}
    candidates = []
    for barrier in repository.list_checkpoint_barriers():
        opening = repository.get_attempt(barrier["opening_attempt_id"])
        if opening.run_id != parent_run_id:
            continue
        plan = plans.get(opening.physical_query_plan_item_id)
        if plan is None:
            raise ValueError("reconcile barrier缺少父计划")
        related = [required[cid] for cid, linked_plans in linked.items()
                   if cid in required and any(p.plan_item_id == plan.plan_item_id for p in linked_plans)]
        if not related:
            raise ValueError("reconcile barrier缺少coverage关联")
        all_parent_barriers.append((barrier, plan))
        if barrier["barrier_id"] in unresolved_ids:
            for entry in related:
                candidates.append((key(plan, entry, barrier["work_position"], barrier["barrier_id"]),
                                   root(plan), entry, barrier, None))
    priority, strategy = 1, "earliest_unresolved_barrier"
    if not candidates:
        priority, strategy = 2, "unresolved_coverage"
        with_barriers = {root(plan).plan_item_id for _, plan in all_parent_barriers}
        for cid, entry in required.items():
            resolution = resolutions.get(cid)
            if resolution is not None and resolution.status.value == "complete":
                continue
            for plan in linked[cid]:
                if plan.attempt_kind == AttemptKind.DISCOVERY and plan.plan_item_id not in with_barriers:
                    candidates.append((key(plan, entry), plan, entry, None, None))
    if not candidates:
        priority, strategy = 3, "snapshot_integrity_failure"
        for cid, entry in required.items():
            resolution = resolutions.get(cid)
            for snapshot_id in (() if resolution is None else resolution.snapshot_ids):
                events = repository.list_snapshot_integrity_events(snapshot_id)
                if events and events[-1].status.value == "quarantined":
                    for plan in linked[cid]:
                        if plan.attempt_kind == AttemptKind.DISCOVERY:
                            candidates.append((key(plan, entry, identity=snapshot_id), plan, entry, None, snapshot_id))
    if not candidates:
        priority, strategy = 4, "earliest_completed_slice_periodic_history_recheck"
        for cid, entry in required.items():
            if cid in resolutions and resolutions[cid].status.value == "complete":
                for plan in linked[cid]:
                    if plan.attempt_kind == AttemptKind.DISCOVERY:
                        candidates.append((key(plan, entry), plan, entry, None, None))
    if not candidates:
        raise ValueError("reconcile父运行没有尚待修复或已完成的required slice")
    _, plan, entry, barrier, snapshot_id = min(candidates, key=lambda candidate: candidate[0])
    definition = next((d for d in definitions if d.source_definition_id == plan.source_definition_id), None)
    query = None if definition is None else next((q for q in definition.queries if q.query_id == plan.query_id), None)
    if query is None:
        raise ValueError("reconcile目标在当前registry中没有兼容query")
    cutoff = as_of.astimezone(timezone.utc)
    end = min(plan.time_end, cutoff)
    start = plan.time_start - timedelta(days=definition.incremental_policy.overlap_days)
    if query.earliest_available_at is not None:
        start = max(start, query.earliest_available_at)
    floor_source = "source_overlap_and_earliest_available_at"
    if parent.mode == AcquisitionMode.RECONCILE:
        if str(definition.version) != plan.source_definition_version:
            raise ValueError("reconcile父恢复范围缺少当前兼容来源版本")
        floor = _parent_range_floor(repository, parent, plans, plan, frozen_definitions)
        start = max(start, floor)
        if start > plan.time_start:
            raise ValueError("reconcile父恢复范围不得截断精确目标")
        floor_source = f"parent_effective_range:{parent_run_id}"
    if end <= plan.time_start or start >= end:
        raise ValueError("reconcile effective range为空或as_of早于目标")
    refs = {(d.source_definition_id, str(d.version)) for d in definitions if "business_model" in d.scopes}
    target = {
        "parent_run_id": parent_run_id, "resolved_parent_run_id": parent_run_id,
        "strategy": strategy, "selection_priority": priority,
        "source_definition_id": plan.source_definition_id,
        "source_definition_version": str(plan.source_definition_version),
        "query_id": plan.query_id, "partition_key": plan.partition_key,
        "plan_item_id": plan.plan_item_id, "coverage_entry_id": entry.coverage_entry_id,
        "parent_time_start": plan.time_start.isoformat(), "parent_time_end": plan.time_end.isoformat(),
        "work_position": (barrier["work_position"] if barrier else json.dumps({
            "kind": "discovery", "page": 1, "cursor": None, "canonical_resource_id": None,
        }, sort_keys=True, separators=(",", ":"))),
        "barrier_id": None if barrier is None else barrier["barrier_id"],
        "opening_attempt_id": None if barrier is None else barrier["opening_attempt_id"],
        "retry_group_id": None if barrier is None else barrier["retry_group_id"],
        "canonical_resource_id": None if barrier is None else barrier.get("canonical_resource_id"),
        "quarantined_snapshot_id": snapshot_id,
        "effective_range": {"time_start": start.isoformat(), "time_end": end.isoformat()},
        "range_policy_version": RANGE_POLICY_VERSION,
        "range_floor": start.isoformat(), "range_floor_source": floor_source,
        "overlap_days": definition.incremental_policy.overlap_days,
        "start_at": start.isoformat(), "as_of": cutoff.isoformat(),
        "registry_compatibility_review_required": refs != {
            (ref.source_definition_id, str(ref.version)) for ref in parent.source_definition_refs
        },
        "unresolved_barrier_ids": tuple(sorted(row["barrier_id"] for row, _ in all_parent_barriers
                                                if row["barrier_id"] in unresolved_ids)),
        "quarantined_snapshot_ids": () if snapshot_id is None else (snapshot_id,),
    }
    return ReconcileSelection(parent_run_id, start, target)
