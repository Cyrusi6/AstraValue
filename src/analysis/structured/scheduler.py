from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from .storage import StructuredRunContext, StructuredStorage


BEIJING = ZoneInfo("Asia/Shanghai")


class DueKind(str, Enum):
    MARKET = "market"
    CATALOG = "catalog"
    EVENT = "event"
    FINANCIAL = "financial"
    SUMMARY = "summary"
    ON_DEMAND = "on_demand"


@dataclass(frozen=True)
class DueDecision:
    kind: DueKind
    due: bool
    scheduled_for: str | None
    reason_code: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class EventIncrementalWindow:
    start: datetime
    end: datetime
    overlap_days: int
    incomplete_lifecycle_ids: tuple[str, ...]
    limitation: str | None


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    dataset_id: str
    state: str
    attempt_count: int
    failure_count: int
    committed_page_count: int
    committed_record_count: int
    reusable_snapshot_ids: tuple[str, ...]
    next_retry_ordinal: int | None


@dataclass(frozen=True)
class StructuredRunStatus:
    run_id: str
    total_jobs: int
    succeeded: int
    no_data: int
    failed: int
    retryable: int
    pending: int
    partial: int
    jobs: tuple[JobStatus, ...]


def calculate_due_work(
    *,
    now: datetime,
    trading_days: Iterable[date] | None,
    completed_schedule_keys: Iterable[str] = (),
    new_report_periods: Iterable[str] = (),
    completed_report_periods: Iterable[str] = (),
    source_delayed_trade_dates: Iterable[date] = (),
    summary_event_ids: Iterable[str] = (),
    activated_on_demand_ids: Iterable[str] = (),
    weekly_summary_weekday: int = 0,
) -> tuple[DueDecision, ...]:
    """Return one-shot due decisions in Beijing time; this performs no I/O."""

    current = _aware(now).astimezone(BEIJING)
    local_day = current.date()
    completed = set(completed_schedule_keys)
    decisions: list[DueDecision] = []
    delayed = sorted(set(source_delayed_trade_dates))

    if trading_days is None:
        decisions.append(
            DueDecision(
                DueKind.MARKET,
                False,
                None,
                "trading_calendar_missing",
                {"local_date": local_day.isoformat()},
            )
        )
    else:
        known_trading_days = set(trading_days)
        market_key = f"market:{local_day.isoformat()}"
        is_trading_day = local_day in known_trading_days
        market_due = (
            is_trading_day
            and current.timetz().replace(tzinfo=None) >= time(19, 0)
            and market_key not in completed
        )
        reason = (
            "due"
            if market_due
            else "non_trading_day"
            if not is_trading_day
            else "before_cutoff"
            if current.timetz().replace(tzinfo=None) < time(19, 0)
            else "already_completed"
        )
        decisions.append(
            DueDecision(
                DueKind.MARKET,
                market_due,
                market_key if is_trading_day else None,
                reason,
                {
                    "expected_trade_date": local_day.isoformat(),
                    "source_delayed_trade_dates": [item.isoformat() for item in delayed],
                },
            )
        )

    for kind in (DueKind.CATALOG, DueKind.EVENT):
        key = f"{kind.value}:{local_day.isoformat()}"
        reached = current.timetz().replace(tzinfo=None) >= time(20, 30)
        due = reached and key not in completed
        decisions.append(
            DueDecision(
                kind,
                due,
                key,
                "due" if due else "already_completed" if key in completed else "before_cutoff",
                {"local_date": local_day.isoformat()},
            )
        )

    completed_reports = set(completed_report_periods)
    for period in dict.fromkeys(str(item) for item in new_report_periods):
        key = f"financial:{period}"
        due = period not in completed_reports and key not in completed
        decisions.append(
            DueDecision(
                DueKind.FINANCIAL,
                due,
                key,
                "new_report_period" if due else "already_completed",
                {"report_period": period},
            )
        )

    event_ids = tuple(dict.fromkeys(str(item) for item in summary_event_ids))
    week = current.strftime("%G-W%V")
    weekly_key = f"summary:{week}"
    weekly_reached = (
        current.weekday() > weekly_summary_weekday
        or (
            current.weekday() == weekly_summary_weekday
            and current.timetz().replace(tzinfo=None) >= time(20, 30)
        )
    )
    summary_due = bool(event_ids) or (weekly_reached and weekly_key not in completed)
    decisions.append(
        DueDecision(
            DueKind.SUMMARY,
            summary_due,
            weekly_key,
            "event_trigger" if event_ids else "weekly_due" if summary_due else "not_due",
            {"event_ids": list(event_ids), "week": week},
        )
    )

    for dataset_id in dict.fromkeys(str(item) for item in activated_on_demand_ids):
        key = f"on_demand:{dataset_id}"
        decisions.append(
            DueDecision(
                DueKind.ON_DEMAND,
                key not in completed,
                key,
                "activated" if key not in completed else "already_completed",
                {"dataset_id": dataset_id},
            )
        )
    return tuple(decisions)


def event_incremental_window(
    *,
    as_of: datetime,
    last_safe_through: datetime | None,
    has_update_marker: bool,
    incomplete_lifecycle_ids: Iterable[str] = (),
    overlap_days: int = 30,
) -> EventIncrementalWindow:
    if overlap_days < 1:
        raise ValueError("event overlap must be at least one day")
    end = _aware(as_of)
    last = None if last_safe_through is None else _aware(last_safe_through)
    overlap_start = end - timedelta(days=overlap_days)
    if has_update_marker:
        start = last or end
        limitation = None
    else:
        start = overlap_start if last is None else min(last, overlap_start)
        limitation = "silent_revisions_before_overlap_are_not_guaranteed"
    if start > end:
        raise ValueError("last_safe_through cannot be later than as_of")
    return EventIncrementalWindow(
        start,
        end,
        overlap_days,
        tuple(dict.fromkeys(str(item) for item in incomplete_lifecycle_ids)),
        limitation,
    )


class StructuredExecutionBridge:
    """Use the acquisition control plane while enforcing frozen structured context."""

    def __init__(
        self,
        *,
        repository: Any,
        storage: StructuredStorage,
        source_gate: Any,
        snapshot_service: Any,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.source_gate = source_gate
        self.snapshot_service = snapshot_service
        snapshot_namespace = getattr(snapshot_service, "storage_namespace_id", None)
        if snapshot_namespace not in (None, storage.storage_namespace_id):
            raise ValueError("shared snapshot service uses another storage namespace")

    def prepare_execution(
        self,
        run_id: str,
        *,
        expected_pins: Mapping[str, Any] | None = None,
    ) -> StructuredRunContext:
        # Missing context and partial optimistic pins fail here, before callers
        # can enter a source gate or resolve/send a request.
        context = self.storage.get_run_context(run_id, expected_pins=expected_pins)
        run = self.repository.get_run(run_id)
        if str(_value(run, "storage_namespace_id")) != context.storage_namespace_id:
            raise ValueError("shared run namespace differs from structured context")
        if str(_value(run, "ticker")) != context.ticker:
            raise ValueError("shared run ticker differs from structured context")
        return context

    def claim_lease(self, run_id: str, **kwargs: Any) -> tuple[Any, str]:
        self.prepare_execution(run_id)
        return self.repository.claim_lease(run_id, **kwargs)

    def save_attempt(self, attempt: Any, *, owner_token: str) -> None:
        run_id = str(_value(attempt, "run_id"))
        self.prepare_execution(run_id)
        self.repository.save_attempt(attempt, owner_token=owner_token)

    def commit_page_bundle(
        self,
        *,
        page: Mapping[str, Any],
        records: Iterable[Mapping[str, Any]] = (),
        fields: Iterable[Mapping[str, Any]] = (),
        owner_token: str,
        lease_epoch: int,
    ) -> None:
        job = self.storage.get_job(str(page["job_id"]))
        self.prepare_execution(str(job["run_id"]))
        self.storage.commit_page_bundle(
            self.repository,
            page=page,
            records=records,
            fields=fields,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )

    @contextmanager
    def hold_source(
        self,
        *,
        run_id: str,
        source_definition: Any,
        host: str,
        deadline_monotonic: float,
        lease_guard: Callable[..., None],
    ) -> Iterator[Any]:
        self.prepare_execution(run_id)
        upstream = str(_value(source_definition, "upstream_identity"))
        configured = float(
            _value(_value(source_definition, "rate_limit", default={}), "min_interval_seconds")
        )
        minimum = 5.0 if "cninfo" in upstream.lower() else 3.0
        with self.source_gate.hold(
            str(_value(source_definition, "source_definition_id")),
            host,
            upstream_identity=upstream,
            min_interval_seconds=max(configured, minimum),
            deadline_monotonic=deadline_monotonic,
            lease_guard=lease_guard,
        ) as permit:
            yield permit

    def status(self, run_id: str) -> StructuredRunStatus:
        self.prepare_execution(run_id)
        jobs = self.storage.list_jobs(run_id, limit=None)
        attempts = self.repository.list_attempts(run_id=run_id, limit=None)
        attempts_by_plan: dict[str, list[Any]] = {}
        for attempt in attempts:
            attempts_by_plan.setdefault(
                str(_value(attempt, "physical_query_plan_item_id")), []
            ).append(attempt)
        statuses: list[JobStatus] = []
        for job in jobs:
            job_attempts = attempts_by_plan.get(str(job["plan_item_id"]), [])
            terminal_outcomes: list[str] = []
            failure_count = 0
            for attempt in job_attempts:
                events = self.repository.list_attempt_events(str(_value(attempt, "attempt_id")))
                for event in events:
                    outcome = _enum_value(_value(event, "outcome"))
                    if outcome:
                        terminal_outcomes.append(outcome)
                        if outcome not in {"success", "unchanged", "no_data"}:
                            failure_count += 1
            pages = self.storage.list_pages(str(job["job_id"]))
            records = self.storage.list_records(job_id=str(job["job_id"]), limit=None)
            successful = any(outcome in {"success", "unchanged", "no_data"} for outcome in terminal_outcomes)
            has_terminal_page = any(bool(item.get("terminal")) for item in pages)
            if "no_data" in terminal_outcomes:
                state = "no_data"
            elif successful and ("unchanged" in terminal_outcomes or has_terminal_page):
                state = "succeeded"
            elif any(outcome == "partial_success" for outcome in terminal_outcomes):
                state = (
                    "failed"
                    if len(job_attempts) >= int(job["max_attempts"])
                    else "partial"
                )
            elif job_attempts and len(job_attempts) >= int(job["max_attempts"]):
                state = "failed"
            elif job_attempts:
                state = "retryable"
            else:
                state = "pending"
            next_retry = (
                None
                if state in {"succeeded", "no_data", "failed"}
                else max(
                    (int(_value(item, "retry_ordinal", default=0)) for item in job_attempts),
                    default=-1,
                )
                + 1
            )
            statuses.append(
                JobStatus(
                    str(job["job_id"]),
                    str(job["dataset_id"]),
                    state,
                    len(job_attempts),
                    failure_count,
                    len(pages),
                    len(records),
                    self.storage.unprojected_snapshot_ids(str(job["job_id"])),
                    next_retry,
                )
            )
        return StructuredRunStatus(
            run_id,
            len(statuses),
            sum(item.state == "succeeded" for item in statuses),
            sum(item.state == "no_data" for item in statuses),
            sum(item.state == "failed" for item in statuses),
            sum(item.state == "retryable" for item in statuses),
            sum(item.state == "pending" for item in statuses),
            sum(item.state == "partial" for item in statuses),
            tuple(statuses),
        )

    def resume_candidates(self, run_id: str) -> tuple[JobStatus, ...]:
        status = self.status(run_id)
        return tuple(
            item
            for item in status.jobs
            if item.state in {"pending", "retryable", "partial"}
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _value(value: Any, name: str, *, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))
