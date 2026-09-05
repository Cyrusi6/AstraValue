"""Frozen query dependencies and deterministic physical execution order."""
from __future__ import annotations

from collections import defaultdict

from .models import AttemptKind, PhysicalQueryPlanItem, SourceDefinition


def order_dependency_plans(
    plans: tuple[PhysicalQueryPlanItem, ...], definitions: tuple[SourceDefinition, ...],
) -> tuple[PhysicalQueryPlanItem, ...]:
    queries = {
        (definition.source_definition_id, str(definition.version), query.query_id): query
        for definition in definitions for query in definition.queries
    }
    by_query = defaultdict(list)
    for item in plans:
        if item.attempt_kind == AttemptKind.DISCOVERY:
            by_query[(item.source_definition_id, str(item.source_definition_version), item.query_id)].append(item)
    edges = {}
    query_ids = {}
    for item in plans:
        key = (item.source_definition_id, str(item.source_definition_version), item.query_id)
        dependencies = tuple(sorted({binding.source_query_id for binding in queries[key].parameter_bindings.values()}))
        query_ids[item.plan_item_id] = dependencies
        edges[item.plan_item_id] = tuple(
            prerequisite.plan_item_id
            for dependency in dependencies
            for prerequisite in sorted(by_query[(*key[:2], dependency)], key=lambda p: (p.ordinal, p.plan_item_id))
        )
    by_id = {item.plan_item_id: item for item in plans}
    ordered = []
    visiting, visited = set(), set()

    def visit(plan_id):
        if plan_id in visiting:
            raise ValueError("采集 prerequisite 图存在环")
        if plan_id in visited:
            return
        visiting.add(plan_id)
        for prerequisite in edges[plan_id]:
            visit(prerequisite)
        visiting.remove(plan_id)
        visited.add(plan_id)
        ordered.append(by_id[plan_id].model_copy(update={
            "ordinal": len(ordered),
            "prerequisite_query_ids": query_ids[plan_id],
            "prerequisite_plan_item_ids": edges[plan_id],
        }))

    for item in sorted(plans, key=lambda p: (p.ordinal, p.plan_item_id)):
        visit(item.plan_item_id)
    return tuple(ordered)
