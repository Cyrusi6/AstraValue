"""Read-only, authoritative selection of one finalized run's repair target."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .models import AcquisitionRunEventType, AttemptKind, CoveragePlanDisposition
from .repository import AcquisitionNotFoundError


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


def select_reconcile_target(repository, definitions, parent_run_id, *, as_of, now=None):
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
