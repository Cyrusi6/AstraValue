from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from analysis.acquisition.models import (
    AcquisitionPlan,
    CoverageEntry,
    CoveragePlanDisposition,
    PhysicalQueryCoverageLink,
    PhysicalQueryPlanItem,
)

from .storage import (
    StructuredRunContext,
    StructuredStorage,
    canonical_sha256,
    stable_structured_id,
)


class StructuredPlanningError(RuntimeError):
    pass


class HistoryMode(str, Enum):
    ALL_AVAILABLE = "all_available"
    REPORT_CATALOG = "report_catalog"
    CURRENT_SNAPSHOT = "current_snapshot"
    ON_DEMAND = "on_demand"


class PlanDisposition(str, Enum):
    REQUIRED = "required"
    INACTIVE_ON_DEMAND = "inactive_on_demand"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CompanyPlanTarget:
    company_id: str
    ticker: str
    listing_date: date | None = None
    role: str = "target"
    selection_reason: str = "explicit"


@dataclass(frozen=True)
class DatasetWork:
    company_id: str
    ticker: str
    dataset_id: str
    source_definition_id: str
    source_definition_version: str
    query_id: str
    purpose: str
    history_mode: HistoryMode
    disposition: PlanDisposition
    time_start: datetime
    time_end: datetime
    partition_key: str
    parameters: Mapping[str, Any]
    ordinal: int
    reason_code: str | None = None
    reconciles_job_id: str | None = None

    @property
    def scope_key(self) -> str:
        return canonical_sha256(
            {
                "company_id": self.company_id,
                "ticker": self.ticker,
                "dataset_id": self.dataset_id,
                "purpose": self.purpose,
                "time_start": self.time_start,
                "time_end": self.time_end,
                "partition_key": self.partition_key,
                "parameters": self.parameters,
            }
        )


@dataclass(frozen=True)
class ComposedStructuredPlan:
    shared_plan: AcquisitionPlan
    jobs: tuple[dict[str, Any], ...]
    works: tuple[DatasetWork, ...]


class StructuredDatasetPlanner:
    """Zero-network planner for full supplier history and scoped updates."""

    def plan_history(
        self,
        *,
        companies: Iterable[CompanyPlanTarget | Mapping[str, Any]],
        datasets: Iterable[Any],
        as_of: datetime,
        selected_dataset_ids: Iterable[str] | None = None,
        activated_dataset_ids: Iterable[str] = (),
        known_report_periods: Mapping[str, Sequence[str | date | datetime]] | None = None,
    ) -> tuple[DatasetWork, ...]:
        cutoff = _aware_utc(as_of)
        selected = None if selected_dataset_ids is None else set(selected_dataset_ids)
        activated = set(activated_dataset_ids)
        report_periods = known_report_periods or {}
        targets = tuple(_coerce_company(item) for item in companies)
        if not targets:
            raise StructuredPlanningError("structured plan requires an explicit company scope")
        if len({item.company_id for item in targets}) != len(targets):
            raise StructuredPlanningError("structured company scope contains duplicate company ids")

        dataset_values = tuple(datasets)
        dataset_ids = [_text(item, "dataset_id") for item in dataset_values]
        if len(set(dataset_ids)) != len(dataset_ids):
            raise StructuredPlanningError("structured dataset registry contains duplicate ids")
        unknown = set() if selected is None else selected - set(dataset_ids)
        if unknown:
            raise StructuredPlanningError(
                "selected structured datasets are unknown: " + ", ".join(sorted(unknown))
            )

        works: list[DatasetWork] = []
        ordinal = 0
        for target in targets:
            company_type_planned = False
            for dataset in dataset_values:
                dataset_id = _text(dataset, "dataset_id")
                if selected is not None and dataset_id not in selected:
                    continue
                if not _applies(dataset, target):
                    works.append(
                        _static_work(
                            target,
                            dataset,
                            cutoff,
                            ordinal,
                            PlanDisposition.NOT_APPLICABLE,
                            "dataset_not_applicable",
                        )
                    )
                    ordinal += 1
                    continue
                support = str(_value(dataset, "support_status", "status", default="supported"))
                if support not in {"supported", "validated", "executable"}:
                    works.append(
                        _static_work(
                            target,
                            dataset,
                            cutoff,
                            ordinal,
                            PlanDisposition.UNSUPPORTED,
                            f"dataset_{support}",
                        )
                    )
                    ordinal += 1
                    continue
                mode = _history_mode(dataset)
                if mode == HistoryMode.ON_DEMAND and dataset_id not in activated:
                    works.append(
                        _static_work(
                            target,
                            dataset,
                            cutoff,
                            ordinal,
                            PlanDisposition.INACTIVE_ON_DEMAND,
                            "on_demand_not_activated",
                        )
                    )
                    ordinal += 1
                    continue
                if mode == HistoryMode.CURRENT_SNAPSHOT:
                    works.append(
                        _required_work(
                            target,
                            dataset,
                            cutoff,
                            cutoff + timedelta(microseconds=1),
                            "current_snapshot",
                            "current",
                            ordinal,
                        )
                    )
                    ordinal += 1
                    continue
                if mode == HistoryMode.REPORT_CATALOG:
                    if not company_type_planned:
                        works.append(
                            _required_work(
                                target,
                                dataset,
                                cutoff,
                                cutoff + timedelta(microseconds=1),
                                "company_type",
                                "company-type",
                                ordinal,
                            )
                        )
                        ordinal += 1
                        company_type_planned = True
                    works.append(
                        _required_work(
                            target,
                            dataset,
                            _history_start(target, dataset, cutoff),
                            cutoff + timedelta(microseconds=1),
                            "report_catalog",
                            "catalog",
                            ordinal,
                        )
                    )
                    ordinal += 1
                    for period in report_periods.get(dataset_id, ()):
                        period_at = _period_datetime(period)
                        if period_at > cutoff:
                            continue
                        works.append(
                            _required_work(
                                target,
                                dataset,
                                period_at,
                                period_at + timedelta(days=1),
                                "report_period",
                                f"report:{period_at.date().isoformat()}",
                                ordinal,
                                parameters={"report_period": period_at.date().isoformat()},
                            )
                        )
                        ordinal += 1
                    continue
                start = _history_start(target, dataset, cutoff)
                enumeration = str(
                    _value(dataset, "history_enumeration", default="")
                )
                if enumeration == "year_quarter_batches":
                    for year, quarter, window_start, window_end in _quarter_windows(
                        start, cutoff + timedelta(microseconds=1)
                    ):
                        works.append(
                            _required_work(
                                target,
                                dataset,
                                window_start,
                                window_end,
                                "year_quarter",
                                f"quarter:{year}:Q{quarter}",
                                ordinal,
                                parameters={"year": year, "quarter": quarter},
                            )
                        )
                        ordinal += 1
                    continue
                batch_days = _integer(
                    dataset,
                    "batch_days",
                    default=(366 if enumeration == "date_range_batches" else 0),
                )
                for window_start, window_end in _history_windows(
                    start,
                    cutoff + timedelta(microseconds=1),
                    batch_days,
                ):
                    works.append(
                        _required_work(
                            target,
                            dataset,
                            window_start,
                            window_end,
                            "full_history",
                            f"history:{window_start.date().isoformat()}:{window_end.date().isoformat()}",
                            ordinal,
                        )
                    )
                    ordinal += 1
        return tuple(works)

    def compose_shared_plan(
        self,
        *,
        run: Any,
        context: StructuredRunContext,
        works: Iterable[DatasetWork],
        source_definitions: Mapping[tuple[str, str], Any],
    ) -> ComposedStructuredPlan:
        run_id = str(_value(run, "run_id"))
        if run_id != context.run_id:
            raise StructuredPlanningError("structured context does not belong to the run")
        run_namespace = str(_value(run, "storage_namespace_id", default=""))
        if run_namespace and run_namespace != context.storage_namespace_id:
            raise StructuredPlanningError(
                "structured context namespace does not belong to the run"
            )
        run_ticker = str(_value(run, "ticker", default=""))
        if run_ticker and run_ticker != context.ticker:
            raise StructuredPlanningError(
                "structured context ticker does not belong to the run"
            )
        work_values = tuple(works)
        if any(item.company_id != context.company_id for item in work_values):
            raise StructuredPlanningError(
                "one structured run context cannot mix company identities"
            )
        if any(item.ticker != context.ticker for item in work_values):
            raise StructuredPlanningError("one structured run context cannot mix tickers")

        plan_items: list[PhysicalQueryPlanItem] = []
        coverage_entries: list[CoverageEntry] = []
        links: list[PhysicalQueryCoverageLink] = []
        jobs: list[dict[str, Any]] = []
        for work in work_values:
            source_key = (work.source_definition_id, work.source_definition_version)
            source = source_definitions.get(source_key)
            coverage_id = stable_structured_id(
                "coverage", {"run_id": run_id, "scope_key": work.scope_key}
            )
            if work.disposition != PlanDisposition.REQUIRED:
                coverage_entries.append(
                    CoverageEntry(
                        coverage_entry_id=coverage_id,
                        run_id=run_id,
                        source_definition_id=work.source_definition_id,
                        source_definition_version=work.source_definition_version,
                        question_id=f"DATASET.{work.dataset_id}",
                        query_id=work.query_id,
                        plan_disposition=CoveragePlanDisposition.STATIC_POLICY_SKIPPED,
                        static_reason_code="on_demand_supplement"
                        if work.disposition == PlanDisposition.INACTIVE_ON_DEMAND
                        else "source_not_available",
                        time_start=work.time_start,
                        time_end=work.time_end,
                    )
                )
                continue
            if source is None:
                raise StructuredPlanningError(
                    f"missing frozen source definition: {source_key[0]}@{source_key[1]}"
                )
            query = _find_query(source, work.query_id)
            endpoint = _text(query, "endpoint")
            plan_item_id = stable_structured_id(
                "structured-plan", {"run_id": run_id, "scope_key": work.scope_key}
            )
            parameters = dict(_value(query, "parameter_template", default={}) or {})
            parameters.update(work.parameters)
            if context.frozen_config.get("research_scope"):
                from .scope import request_fields
                parameters = request_fields(
                    work.dataset_id,
                    parameters,
                    context.frozen_config.get('industry_profile_id'),
                    research_profile_id=context.frozen_config.get('research_profile_id'),
                )
            parameters.setdefault("ticker", work.ticker)
            parameters.setdefault("company_id", work.company_id)
            pagination = _value(query, "pagination", default={}) or {}
            pagination_fingerprint = canonical_sha256(_to_mapping(pagination))
            item = PhysicalQueryPlanItem(
                plan_item_id=plan_item_id,
                run_id=run_id,
                source_definition_id=work.source_definition_id,
                source_definition_version=work.source_definition_version,
                query_id=work.query_id,
                query_family=str(_value(query, "query_family", default=work.dataset_id)),
                execution_key=f"structured:{work.dataset_id}:{work.purpose}",
                request_method=str(_value(query, "request_method", default="GET")),
                request_encoding=str(_value(query, "request_encoding", default="query")),
                fixed_headers=dict(_value(query, "fixed_headers", default={}) or {}),
                parameter_binding_names=tuple(
                    sorted((_value(query, "parameter_bindings", default={}) or {}).keys())
                ),
                prerequisite_query_ids=tuple(
                    _value(query, "prerequisite_query_ids", default=()) or ()
                ),
                endpoint=endpoint,
                normalized_parameters=parameters,
                partition_key=work.partition_key,
                pagination_fingerprint=pagination_fingerprint,
                ordinal=work.ordinal,
                time_start=work.time_start,
                time_end=work.time_end,
            )
            coverage = CoverageEntry(
                coverage_entry_id=coverage_id,
                run_id=run_id,
                source_definition_id=work.source_definition_id,
                source_definition_version=work.source_definition_version,
                question_id=f"DATASET.{work.dataset_id}",
                query_id=work.query_id,
                plan_disposition=CoveragePlanDisposition.REQUIRED,
                time_start=work.time_start,
                time_end=work.time_end,
            )
            link = PhysicalQueryCoverageLink(
                plan_item_id=plan_item_id,
                coverage_entry_id=coverage_id,
            )
            dedupe_key = canonical_sha256(
                {
                    "namespace": context.storage_namespace_id,
                    "company_id": context.company_id,
                    "dataset_id": work.dataset_id,
                    "scope_key": work.scope_key,
                    # Report-period jobs are materialized from each catalog
                    # refresh.  Keep ordinary historical jobs globally
                    # idempotent, while allowing a later run to refresh the
                    # same latest periods without colliding with the prior
                    # run's UNIQUE(dedupe_key) row.
                    "run_id": run_id if work.purpose == "report_period" else None,
                    "dataset_registry_hash": context.dataset_registry_hash,
                    "field_registry_hash": context.field_registry_hash,
                    "query_pack_hash": context.query_pack_hash,
                    "source_registry_hash": context.source_registry_hash,
                    "policy_version": context.policy_version,
                    # A company deferral changes the frozen execution scope;
                    # do not bind its jobs to an older full-scope run.
                    **({"acquisition_deferral": context.frozen_config["acquisition_deferral"]}
                       if context.frozen_config.get("acquisition_deferral") else {}),
                    **({"reconcile_run_id": run_id} if work.reconciles_job_id else {}),
                }
            )
            jobs.append(
                {
                    "job_id": stable_structured_id("structured-job", dedupe_key),
                    "run_id": run_id,
                    "plan_item_id": plan_item_id,
                    "storage_namespace_id": context.storage_namespace_id,
                    "company_id": context.company_id,
                    "ticker": context.ticker,
                    "dataset_id": work.dataset_id,
                    "source_definition_id": work.source_definition_id,
                    "source_definition_version": work.source_definition_version,
                    "purpose": work.purpose,
                    "schedule_mode": work.history_mode.value,
                    "scope_key": work.scope_key,
                    "dedupe_key": dedupe_key,
                    "time_start": work.time_start.isoformat(),
                    "time_end": work.time_end.isoformat(),
                    "max_attempts": int(
                        _value(source, "retry_policy", default={}).max_attempts
                        if hasattr(_value(source, "retry_policy", default={}), "max_attempts")
                        else _value(
                            _value(source, "retry_policy", default={}),
                            "max_attempts",
                            default=2,
                        )
                    ),
                    "ordinal": work.ordinal,
                    **({"reconciles_job_id": work.reconciles_job_id}
                       if work.reconciles_job_id else {}),
                    "created_at": _aware_utc(
                        _value(run, "created_at", default=datetime.now(timezone.utc))
                    ).isoformat(),
                }
            )
            plan_items.append(item)
            coverage_entries.append(coverage)
            links.append(link)
        shared = AcquisitionPlan(
            run=run,
            coverage_entries=tuple(coverage_entries),
            physical_query_plan_items=tuple(plan_items),
            coverage_links=tuple(links),
        )
        return ComposedStructuredPlan(shared, tuple(jobs), work_values)

    @staticmethod
    def persist(
        storage: StructuredStorage,
        repository: Any,
        *,
        context: StructuredRunContext,
        plan: ComposedStructuredPlan,
    ) -> tuple[str, bool]:
        existing_runs = {
            existing
            for job in plan.jobs
            if (existing := storage.find_run_for_dedupe_key(job["dedupe_key"])) is not None
        }
        if existing_runs:
            if len(existing_runs) != 1:
                raise StructuredPlanningError(
                    "idempotency keys unexpectedly resolve to multiple existing runs"
                )
            existing_run = next(iter(existing_runs))
            if any(
                storage.find_run_for_dedupe_key(job["dedupe_key"]) != existing_run
                for job in plan.jobs
            ):
                raise StructuredPlanningError(
                    "plan partially overlaps an existing structured run"
                )
            return existing_run, False
        storage.persist_plan_bundle(
            repository,
            run=plan.shared_plan.run,
            context=context,
            plan_items=plan.shared_plan.physical_query_plan_items,
            coverage_entries=plan.shared_plan.coverage_entries,
            links=plan.shared_plan.coverage_links,
            jobs=plan.jobs,
        )
        return context.run_id, True


def _static_work(
    target: CompanyPlanTarget,
    dataset: Any,
    cutoff: datetime,
    ordinal: int,
    disposition: PlanDisposition,
    reason_code: str,
) -> DatasetWork:
    return DatasetWork(
        target.company_id,
        target.ticker,
        _text(dataset, "dataset_id"),
        _source_definition_id(dataset),
        _source_definition_version(dataset),
        str(_value(dataset, "query_id", default=_text(dataset, "dataset_id"))),
        "no_io",
        _history_mode(dataset),
        disposition,
        cutoff,
        cutoff + timedelta(microseconds=1),
        f"{target.company_id}:{_text(dataset, 'dataset_id')}:no-io",
        {},
        ordinal,
        reason_code,
    )


def _required_work(
    target: CompanyPlanTarget,
    dataset: Any,
    start: datetime,
    end: datetime,
    purpose: str,
    partition: str,
    ordinal: int,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> DatasetWork:
    return DatasetWork(
        target.company_id,
        target.ticker,
        _text(dataset, "dataset_id"),
        _source_definition_id(dataset),
        _source_definition_version(dataset),
        str(_value(dataset, "query_id", default=_text(dataset, "dataset_id"))),
        purpose,
        _history_mode(dataset),
        PlanDisposition.REQUIRED,
        start,
        end,
        f"{target.company_id}:{_text(dataset, 'dataset_id')}:{partition}",
        dict(parameters or {}),
        ordinal,
    )


def _history_start(
    target: CompanyPlanTarget,
    dataset: Any,
    cutoff: datetime,
) -> datetime:
    configured = _value(dataset, "earliest_available_at", "history_start")
    options = [value for value in (target.listing_date, _to_date(configured)) if value]
    if not options:
        raise StructuredPlanningError(
            f"dataset {_text(dataset, 'dataset_id')} has no auditable history start"
        )
    selected = max(options) if bool(_value(dataset, "post_listing_only", default=True)) else min(options)
    result = datetime.combine(selected, time.min, tzinfo=timezone.utc)
    if result > cutoff:
        raise StructuredPlanningError("history start is later than plan as_of")
    return result


def _history_windows(
    start: datetime,
    end: datetime,
    batch_days: int,
) -> Iterable[tuple[datetime, datetime]]:
    if end <= start:
        return ()
    if batch_days <= 0:
        return ((start, end),)
    values: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        following = min(cursor + timedelta(days=batch_days), end)
        values.append((cursor, following))
        cursor = following
    return tuple(values)


def _quarter_windows(
    start: datetime,
    end: datetime,
) -> Iterable[tuple[int, int, datetime, datetime]]:
    """Enumerate every provider year/quarter intersecting the frozen range."""

    if end <= start:
        return ()
    quarter = (start.month - 1) // 3 + 1
    cursor = datetime(start.year, (quarter - 1) * 3 + 1, 1, tzinfo=timezone.utc)
    values: list[tuple[int, int, datetime, datetime]] = []
    while cursor < end:
        next_year = cursor.year + (1 if cursor.month == 10 else 0)
        next_month = 1 if cursor.month == 10 else cursor.month + 3
        following = datetime(next_year, next_month, 1, tzinfo=timezone.utc)
        window_start = max(start, cursor)
        window_end = min(end, following)
        if window_end > window_start:
            values.append(
                (
                    cursor.year,
                    (cursor.month - 1) // 3 + 1,
                    window_start,
                    window_end,
                )
            )
        cursor = following
    return tuple(values)


def _find_query(source: Any, query_id: str) -> Any:
    queries = _value(source, "queries", default=()) or ()
    for query in queries:
        if str(_value(query, "query_id")) == query_id:
            return query
    raise StructuredPlanningError(
        f"frozen source definition has no query: {query_id}"
    )


def _applies(dataset: Any, target: CompanyPlanTarget) -> bool:
    applicability = _value(dataset, "applicability", default={}) or {}
    tickers = set(_value(applicability, "tickers", default=()) or ())
    excluded = set(_value(applicability, "excluded_tickers", default=()) or ())
    if target.ticker in excluded:
        return False
    return not tickers or target.ticker in tickers


def _history_mode(dataset: Any) -> HistoryMode:
    raw = str(_value(dataset, "history_mode", "history", default="all_available"))
    enumeration = str(_value(dataset, "history_enumeration", default=""))
    if raw in {"on_demand", "on_demand_all_available_history"}:
        return HistoryMode.ON_DEMAND
    if raw in {"current_snapshot", "snapshot_from_first_retrieval"} or enumeration == "current_snapshot":
        return HistoryMode.CURRENT_SNAPSHOT
    if raw == "report_catalog" or enumeration == "financial_date_catalog":
        return HistoryMode.REPORT_CATALOG
    if raw in {"all_available", "all_available_history"}:
        return HistoryMode.ALL_AVAILABLE
    raise StructuredPlanningError(f"unknown structured history mode: {raw}")


def _source_definition_id(dataset: Any) -> str:
    explicit = _value(dataset, "source_definition_id", "source_id")
    if explicit:
        return str(explicit)
    provider = _value(dataset, "provider")
    if provider:
        return f"structured-{provider}"
    raise StructuredPlanningError("dataset is missing source definition identity")


def _source_definition_version(dataset: Any) -> str:
    return str(_value(dataset, "source_definition_version", "source_version", default="1.0.0"))


def _coerce_company(value: CompanyPlanTarget | Mapping[str, Any]) -> CompanyPlanTarget:
    if isinstance(value, CompanyPlanTarget):
        return value
    return CompanyPlanTarget(
        company_id=_text(value, "company_id"),
        ticker=_text(value, "ticker"),
        listing_date=_to_date(_value(value, "listing_date")),
        role=str(_value(value, "role", default="target")),
        selection_reason=str(_value(value, "selection_reason", default="explicit")),
    )


def _value(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _text(value: Any, *names: str) -> str:
    found = _value(value, *names)
    if found is None or not str(found).strip():
        raise StructuredPlanningError(f"structured dataset is missing {'/'.join(names)}")
    return str(found)


def _integer(value: Any, name: str, *, default: int) -> int:
    found = _value(value, name, default=default)
    return int(found or 0)


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _period_datetime(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        return _aware_utc(value)
    parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return datetime.combine(parsed, time.min, tzinfo=timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StructuredPlanningError("structured planner timestamps must be aware")
    return value.astimezone(timezone.utc)


def _to_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=False)
    return vars(value)
