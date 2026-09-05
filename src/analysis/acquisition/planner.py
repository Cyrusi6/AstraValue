from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .dependencies import order_dependency_plans

from .models import (
    AcquisitionMode,
    AcquisitionPlan,
    AcquisitionRun,
    AcquisitionRunKind,
    BusinessQuestion,
    CompanyAcquisitionProfile,
    CoverageEntry,
    CoveragePlanDisposition,
    PhysicalQueryCoverageLink,
    PhysicalQueryPlanItem,
    SourceDefinition,
    SourceDefinitionRef,
    SourcePolicyStatus,
    SourceQueryDefinition,
    stable_acquisition_id,
    canonical_json_sha256,
)
from .registry import (
    LoadedQuestionSet,
    LoadedSourceRegistry,
    SourceRegistryError,
    SourceRegistryLoader,
)


class AcquisitionPlanningError(RuntimeError):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AcquisitionPlanningError("计划时间必须包含时区")
    return value.astimezone(timezone.utc)


def _local_day_start(value: date, timezone_name: str) -> datetime:
    return datetime.combine(value, time.min, tzinfo=ZoneInfo(timezone_name)).astimezone(
        timezone.utc
    )


def _replace_template(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for key, replacement in replacements.items():
            result = result.replace("{" + key + "}", replacement)
        return result
    if isinstance(value, list):
        return [_replace_template(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_template(item, replacements) for item in value)
    if isinstance(value, dict):
        return {
            key: _replace_template(item, replacements)
            for key, item in sorted(value.items())
        }
    return value


def _window_slices(
    start: datetime,
    end: datetime,
    max_window_days: int | None,
) -> tuple[tuple[datetime, datetime], ...]:
    if end <= start:
        return ()
    if max_window_days is None:
        return ((start, end),)
    result: list[tuple[datetime, datetime]] = []
    cursor = start
    window = timedelta(days=max_window_days)
    while cursor < end:
        next_cursor = min(cursor + window, end)
        result.append((cursor, next_cursor))
        cursor = next_cursor
    return tuple(result)


class AcquisitionPlanner:
    """Deterministically build coverage first, then deduplicated physical I/O."""

    def __init__(
        self,
        registry: LoadedSourceRegistry,
        questions: LoadedQuestionSet,
    ) -> None:
        self.registry = registry
        self.questions = questions
        try:
            SourceRegistryLoader.validate_traceability(
                registry.registry,
                questions.question_set,
            )
        except SourceRegistryError as exc:
            raise AcquisitionPlanningError(str(exc)) from exc

    def plan(
        self,
        profile: CompanyAcquisitionProfile,
        *,
        mode: AcquisitionMode | str,
        as_of: datetime,
        run_kind: AcquisitionRunKind | str = AcquisitionRunKind.PRODUCTION,
        run_id: str | None = None,
        created_at: datetime | None = None,
        start_at: datetime | None = None,
        parent_run_id: str | None = None,
        reconcile_target: dict[str, Any] | None = None,
        storage_namespace_id: str | None = None,
    ) -> AcquisitionPlan:
        try:
            selected_mode = AcquisitionMode(mode)
            selected_kind = AcquisitionRunKind(run_kind)
        except ValueError as exc:
            raise AcquisitionPlanningError(str(exc)) from exc
        cutoff = _as_utc(as_of)
        created = _as_utc(created_at) if created_at is not None else datetime.now(timezone.utc)
        if selected_mode == AcquisitionMode.BASELINE:
            if start_at is not None:
                raise AcquisitionPlanningError(
                    "baseline范围由公司锚点与来源最早可得日决定，不接受调用方start_at"
                )
            try:
                company_start_date, anchor_quality = profile.history_start()
            except ValueError as exc:
                raise AcquisitionPlanningError(str(exc)) from exc
        else:
            if start_at is None:
                raise AcquisitionPlanningError(
                    f"{selected_mode.value}必须由安全checkpoint/reconcile目标提供start_at"
                )
            company_start_date = _as_utc(start_at).date()
            anchor_quality = "checkpoint" if selected_mode == AcquisitionMode.INCREMENTAL else "reconcile_target"
        if selected_mode == AcquisitionMode.RECONCILE and not parent_run_id:
            raise AcquisitionPlanningError("reconcile必须关联parent run")
        if selected_mode != AcquisitionMode.RECONCILE and reconcile_target is not None:
            raise AcquisitionPlanningError("只有reconcile可以携带reconcile_target")

        business_definitions = tuple(
            definition
            for definition in self.registry.registry.definitions
            if "business_model" in definition.scopes
            and definition.scope_version == "v1"
        )
        if not business_definitions:
            raise AcquisitionPlanningError("注册表没有business_model v1来源")
        source_refs = tuple(
            SourceDefinitionRef(
                source_definition_id=definition.source_definition_id,
                version=definition.version,
                content_hash=self.registry.source_definition_hashes[
                    (definition.source_definition_id, definition.version)
                ],
            )
            for definition in business_definitions
        )
        run = AcquisitionRun(
            **({"run_id": run_id} if run_id is not None else {}),
            ticker=profile.ticker,
            company_name=profile.company_name,
            mode=selected_mode,
            run_kind=selected_kind,
            as_of=cutoff,
            created_at=created,
            registry_id=self.registry.registry.registry_id,
            registry_version=self.registry.registry.registry_version,
            registry_content_hash=self.registry.content_hash,
            question_set_id=self.questions.question_set.question_set_id,
            question_set_version=self.questions.question_set.version,
            question_set_content_hash=self.questions.content_hash,
            source_definition_refs=source_refs,
            request_scope=("ad_hoc" if selected_kind == AcquisitionRunKind.AD_HOC else "complete"),
            parent_run_id=parent_run_id,
            reconcile_target=reconcile_target,
            company_anchor_date=company_start_date,
            company_anchor_quality=anchor_quality,
            storage_namespace_id=storage_namespace_id,
            http_route_policy="direct-v1",
        )

        if selected_mode == AcquisitionMode.RECONCILE and reconcile_target and reconcile_target.get("plan_item_id"):
            return self._targeted_reconcile_plan(run, profile, business_definitions, reconcile_target)

        coverage: list[CoverageEntry] = []
        physical_by_key: dict[str, PhysicalQueryPlanItem] = {}
        links: list[PhysicalQueryCoverageLink] = []
        ordinal = 0
        questions = self.questions.question_set.topics
        for definition in business_definitions:
            try:
                SourceRegistryLoader.assert_effective(definition, cutoff)
            except SourceRegistryError as exc:
                raise AcquisitionPlanningError(
                    f"来源定义对run as_of无效: "
                    f"{definition.source_definition_id}@{definition.version}: {exc}"
                ) from exc
            source_start = (
                _as_utc(start_at)
                if selected_mode != AcquisitionMode.BASELINE and start_at is not None
                else _local_day_start(company_start_date, definition.source_timezone)
            )
            if cutoff <= source_start:
                raise AcquisitionPlanningError(
                    f"as_of不晚于采集起点: {definition.source_definition_id}"
                )
            static_reason = self._source_static_reason(definition, profile)
            query_by_question = self._queries_by_question(definition, questions)
            for question in questions:
                relevant_queries = query_by_question[question.question_id]
                if static_reason is not None:
                    if relevant_queries:
                        for query in relevant_queries:
                            coverage.append(
                                self._coverage_entry(
                                    run.run_id,
                                    definition,
                                    question,
                                    query.query_id,
                                    source_start,
                                    cutoff,
                                    CoveragePlanDisposition.STATIC_POLICY_SKIPPED,
                                    static_reason,
                                )
                            )
                    else:
                        coverage.append(
                            self._coverage_entry(
                                run.run_id,
                                definition,
                                question,
                                "__no_relevant_query__",
                                source_start,
                                cutoff,
                                CoveragePlanDisposition.STATIC_POLICY_SKIPPED,
                                static_reason,
                            )
                        )
                    continue
                if not relevant_queries:
                    coverage.append(
                        self._coverage_entry(
                            run.run_id,
                            definition,
                            question,
                            "__no_relevant_query__",
                            source_start,
                            cutoff,
                            CoveragePlanDisposition.STATIC_POLICY_SKIPPED,
                            "no_relevant_query",
                        )
                    )
                    continue
                for query in relevant_queries:
                    effective_start = max(
                        source_start,
                        query.earliest_available_at or source_start,
                    )
                    if effective_start > source_start:
                        unavailable_end = min(effective_start, cutoff)
                        if unavailable_end > source_start:
                            coverage.append(
                                self._coverage_entry(
                                    run.run_id,
                                    definition,
                                    question,
                                    query.query_id,
                                    source_start,
                                    unavailable_end,
                                    CoveragePlanDisposition.STATIC_POLICY_SKIPPED,
                                    "source_not_available",
                                )
                            )
                    for slice_start, slice_end in _window_slices(
                        max(effective_start, source_start),
                        cutoff,
                        query.max_window_days,
                    ):
                        entry = self._coverage_entry(
                            run.run_id,
                            definition,
                            question,
                            query.query_id,
                            slice_start,
                            slice_end,
                            CoveragePlanDisposition.REQUIRED,
                            None,
                        )
                        coverage.append(entry)
                        execution_key = self._physical_execution_key(
                            profile,
                            definition,
                            query,
                            slice_start,
                            slice_end,
                        )
                        plan_item = physical_by_key.get(execution_key)
                        if plan_item is None:
                            plan_item = self._physical_plan_item(
                                run.run_id,
                                profile,
                                definition,
                                query,
                                slice_start,
                                slice_end,
                                execution_key,
                                ordinal,
                            )
                            physical_by_key[execution_key] = plan_item
                            ordinal += 1
                        links.append(
                            PhysicalQueryCoverageLink(
                                plan_item_id=plan_item.plan_item_id,
                                coverage_entry_id=entry.coverage_entry_id,
                            )
                        )

        coverage_sorted = tuple(
            sorted(
                coverage,
                key=lambda item: (
                    item.source_definition_id,
                    item.question_id,
                    item.query_id,
                    item.time_start,
                    item.time_end,
                    item.coverage_entry_id,
                ),
            )
        )
        plan_sorted = order_dependency_plans(tuple(physical_by_key.values()), business_definitions)
        links_sorted = tuple(
            sorted(links, key=lambda item: item.identity)
        )
        return AcquisitionPlan(
            run=run,
            coverage_entries=coverage_sorted,
            physical_query_plan_items=plan_sorted,
            coverage_links=links_sorted,
        )

    def _targeted_reconcile_plan(self, run, profile, definitions, target):
        definition = next(d for d in definitions if d.source_definition_id == target["source_definition_id"])
        SourceRegistryLoader.assert_effective(definition, run.as_of)
        if self._source_static_reason(definition, profile) is not None:
            raise AcquisitionPlanningError("reconcile目标来源当前不可用")
        queries = {q.query_id: q for q in definition.queries}
        selected_ids = set()

        def include(query_id):
            if query_id in selected_ids:
                return
            selected_ids.add(query_id)
            for binding in queries[query_id].parameter_bindings.values():
                include(binding.source_query_id)

        include(target["query_id"])
        start = datetime.fromisoformat(target["effective_range"]["time_start"])
        end = datetime.fromisoformat(target["effective_range"]["time_end"])
        parent_start = datetime.fromisoformat(target["parent_time_start"])
        topics = {topic.question_id: topic for topic in self.questions.question_set.topics}
        coverage, plans, links = [], [], []
        for query in definition.queries:
            if query.query_id not in selected_ids:
                continue
            query_start = max(start, query.earliest_available_at or start)
            # Preserve the parent slice separately from overlap so pagination
            # and same-position barrier resolution retain identical semantics.
            boundaries = [(query_start, end)]
            if query.query_id == target["query_id"] and query_start < parent_start < end:
                boundaries = [(query_start, parent_start), (parent_start, end)]
            for lower, upper in boundaries:
                for lower, upper in _window_slices(lower, upper, query.max_window_days):
                    execution_key = self._physical_execution_key(profile, definition, query, lower, upper)
                    plan = self._physical_plan_item(run.run_id, profile, definition, query,
                                                    lower, upper, execution_key, len(plans))
                    plans.append(plan)
                    for question_id in query.question_ids:
                        entry = self._coverage_entry(run.run_id, definition, topics[question_id],
                            query.query_id, lower, upper, CoveragePlanDisposition.REQUIRED, None)
                        coverage.append(entry)
                        links.append(PhysicalQueryCoverageLink(plan_item_id=plan.plan_item_id,
                                                              coverage_entry_id=entry.coverage_entry_id))
        return AcquisitionPlan(run=run, coverage_entries=tuple(coverage),
            physical_query_plan_items=order_dependency_plans(tuple(plans), definitions),
            coverage_links=tuple(links))

    @staticmethod
    def _source_static_reason(
        definition: SourceDefinition,
        profile: CompanyAcquisitionProfile,
    ) -> str | None:
        if not definition.applies_to(profile.ticker, profile.market):
            return "market_not_applicable"
        if definition.policy_status == SourcePolicyStatus.PENDING_POLICY:
            return "pending_policy"
        if not definition.enabled:
            return "source_disabled"
        return None

    @staticmethod
    def _queries_by_question(
        definition: SourceDefinition,
        questions: tuple[BusinessQuestion, ...],
    ) -> dict[str, tuple[SourceQueryDefinition, ...]]:
        return {
            question.question_id: tuple(
                query
                for query in definition.queries
                if question.question_id in query.question_ids
            )
            for question in questions
        }

    @staticmethod
    def _coverage_entry(
        run_id: str,
        definition: SourceDefinition,
        question: BusinessQuestion,
        query_id: str,
        start: datetime,
        end: datetime,
        disposition: CoveragePlanDisposition,
        reason: str | None,
    ) -> CoverageEntry:
        identity = {
            "run_id": run_id,
            "source_definition_id": definition.source_definition_id,
            "source_definition_version": definition.version,
            "question_id": question.question_id,
            "query_id": query_id,
            "time_start": start.isoformat(),
            "time_end": end.isoformat(),
            "plan_disposition": disposition.value,
            "static_reason_code": reason,
        }
        return CoverageEntry(
            coverage_entry_id=stable_acquisition_id("coverage", identity),
            run_id=run_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            question_id=question.question_id,
            query_id=query_id,
            time_start=start,
            time_end=end,
            plan_disposition=disposition,
            static_reason_code=reason,
        )

    @staticmethod
    def _physical_execution_key(
        profile: CompanyAcquisitionProfile,
        definition: SourceDefinition,
        query: SourceQueryDefinition,
        start: datetime,
        end: datetime,
    ) -> str:
        parameters = AcquisitionPlanner._normalized_parameters(
            profile,
            query,
            start,
            end,
        )
        identity = {
            "source_definition_id": definition.source_definition_id,
            "source_definition_version": definition.version,
            "query_execution_key": query.execution_key,
            "request_method": query.request_method,
            "request_encoding": query.request_encoding,
            "fixed_headers": query.fixed_headers,
            "parameter_bindings": {
                key: value.model_dump(mode="json")
                for key, value in sorted(query.parameter_bindings.items())
            },
            "endpoint": query.endpoint,
            "normalized_parameters": parameters,
            "partition_key": query.partition_key,
            "time_start": start.isoformat(),
            "time_end": end.isoformat(),
            "pagination": query.pagination.model_dump(mode="json"),
        }
        return stable_acquisition_id("exec", identity)

    @staticmethod
    def _normalized_parameters(
        profile: CompanyAcquisitionProfile,
        query: SourceQueryDefinition,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        replacements = {
            "ticker": profile.ticker,
            "market": profile.market.lower(),
            "plate": "sh" if profile.market == "SSE" else "sz",
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
        }
        return _replace_template(query.parameter_template, replacements)

    @staticmethod
    def _physical_plan_item(
        run_id: str,
        profile: CompanyAcquisitionProfile,
        definition: SourceDefinition,
        query: SourceQueryDefinition,
        start: datetime,
        end: datetime,
        execution_key: str,
        ordinal: int,
    ) -> PhysicalQueryPlanItem:
        if query.endpoint is None:
            raise AcquisitionPlanningError(
                f"启用query缺少endpoint: {definition.source_definition_id}/{query.query_id}"
            )
        parameters = AcquisitionPlanner._normalized_parameters(
            profile,
            query,
            start,
            end,
        )
        plan_identity = {
            "run_id": run_id,
            "execution_key": execution_key,
        }
        return PhysicalQueryPlanItem(
            plan_item_id=stable_acquisition_id("plan", plan_identity),
            run_id=run_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            query_id=query.query_id,
            query_family=query.query_family,
            execution_key=execution_key,
            request_method=query.request_method,
            request_encoding=query.request_encoding,
            fixed_headers=query.fixed_headers,
            parameter_binding_names=tuple(sorted(query.parameter_bindings)),
            endpoint=query.endpoint,
            normalized_parameters=parameters,
            partition_key=query.partition_key,
            pagination_fingerprint=canonical_json_sha256(query.pagination),
            time_start=start,
            time_end=end,
            ordinal=ordinal,
        )


def build_acquisition_plan(
    profile: CompanyAcquisitionProfile,
    registry: LoadedSourceRegistry,
    questions: LoadedQuestionSet,
    *,
    mode: AcquisitionMode | str,
    as_of: datetime,
    **kwargs: Any,
) -> AcquisitionPlan:
    return AcquisitionPlanner(registry, questions).plan(
        profile,
        mode=mode,
        as_of=as_of,
        **kwargs,
    )


__all__ = [
    "AcquisitionPlanner",
    "AcquisitionPlanningError",
    "build_acquisition_plan",
]
