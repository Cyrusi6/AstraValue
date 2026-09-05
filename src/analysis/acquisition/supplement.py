"""Explicit, gap-scoped supplementary acquisition; never clears a primary barrier."""
from .dependencies import order_dependency_plans
from .models import AcquisitionPlan, CoveragePlanDisposition, PhysicalQueryCoverageLink
from .planner import AcquisitionPlanningError, _window_slices


def plan_supplement(runtime, *, parent_run_id, coverage_entry_id, source_definition_id, persist=True):
    repo = runtime.repository
    parent = repo.get_run(parent_run_id)
    if not any(e.event_type.value == "finalized" for e in repo.list_run_events(parent_run_id)):
        raise AcquisitionPlanningError("补缺父运行必须已终结")
    entry = repo.get_coverage_entry(coverage_entry_id)
    if entry.run_id != parent_run_id or entry.plan_disposition != CoveragePlanDisposition.REQUIRED:
        raise AcquisitionPlanningError("补缺必须引用父运行的实际必需覆盖缺口")
    resolutions = [r for r in repo.list_coverage_resolutions(parent_run_id)
                   if r.coverage_entry_id == coverage_entry_id]
    if not resolutions or all(r.material_gap_count == 0 for r in resolutions):
        raise AcquisitionPlanningError("覆盖已完成，不存在待补缺口")
    definition = runtime.loaded_registry.definition(source_definition_id)
    profile = runtime.build_profile(parent.ticker, company_name=parent.company_name)
    if (definition.collection_role != "on_demand" or not definition.enabled
            or definition.supplements_source_id != entry.source_definition_id
            or not definition.applies_to(parent.ticker, profile.market)):
        raise AcquisitionPlanningError("目标来源不是该缺口的已启用适用补充来源")
    primary = repo.get_source_definition_version(entry.source_definition_id, entry.source_definition_version)
    original_query = next(q for q in primary.queries if q.query_id == entry.query_id)
    queries = [q for q in definition.queries if q.query_family == original_query.query_family
               and entry.question_id in q.question_ids]
    if not queries:
        raise AcquisitionPlanningError("补充来源当前协议不支持该材料查询类型")
    cutoff = runtime.clock()
    base = runtime.create_plan(profile, mode="reconcile", run_kind="ad_hoc", as_of=cutoff,
        start_at=entry.time_start, parent_run_id=parent_run_id, persist=False)
    run = base.run.model_copy(update={"reconcile_target": {
        "strategy": "on_demand_supplement", "primary_source_definition_id": entry.source_definition_id,
        "source_definition_id": source_definition_id, "coverage_entry_id": coverage_entry_id,
        "time_start": entry.time_start.isoformat(), "time_end": entry.time_end.isoformat(),
        "primary_barrier_resolution": False,
    }})
    topic = next(t for t in runtime.loaded_questions.question_set.topics if t.question_id == entry.question_id)
    plans, coverage, links = [], [], []
    selected = {q.query_id for q in queries}
    by_id = {q.query_id: q for q in definition.queries}
    def include_dependencies(query):
        for binding in query.parameter_bindings.values():
            if binding.source_query_id not in selected:
                selected.add(binding.source_query_id)
                include_dependencies(by_id[binding.source_query_id])
    for query in queries:
        include_dependencies(query)
    for query in definition.queries:
        if query.query_id not in selected:
            continue
        start = max(entry.time_start, query.earliest_available_at or entry.time_start)
        for lower, upper in _window_slices(start, min(entry.time_end, cutoff), query.max_window_days):
            key = runtime.planner._physical_execution_key(profile, definition, query, lower, upper)
            plan = runtime.planner._physical_plan_item(run.run_id, profile, definition, query, lower, upper, key, len(plans))
            covered = runtime.planner._coverage_entry(run.run_id, definition, topic, query.query_id,
                lower, upper, CoveragePlanDisposition.REQUIRED, None)
            plans.append(plan); coverage.append(covered)
            links.append(PhysicalQueryCoverageLink(plan_item_id=plan.plan_item_id, coverage_entry_id=covered.coverage_entry_id))
    if not plans:
        raise AcquisitionPlanningError("缺口时间范围超出补充来源当前可得边界")
    result = AcquisitionPlan(run=run, physical_query_plan_items=order_dependency_plans(tuple(plans), (definition,)),
                            coverage_entries=tuple(coverage), coverage_links=tuple(links))
    if persist:
        runtime.orchestrator.persist_plan(result)
    return result
