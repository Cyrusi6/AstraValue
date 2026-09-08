from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from analysis.structured.scheduler import (
    DueKind,
    StructuredExecutionBridge,
    calculate_due_work,
    event_incremental_window,
)


def _decision(values, kind):
    return next(item for item in values if item.kind == kind)


def test_beijing_cutoffs_use_local_date_across_utc_boundary():
    # 11:01 UTC is 19:01 in Beijing on the same local date.
    values = calculate_due_work(
        now=datetime(2026, 9, 8, 11, 1, tzinfo=timezone.utc),
        trading_days={date(2026, 9, 8)},
    )
    assert _decision(values, DueKind.MARKET).due is True
    assert _decision(values, DueKind.CATALOG).due is False

    # 16:31 UTC belongs to the following Beijing date and must not schedule
    # the prior day's 20:30 work again.
    next_day = calculate_due_work(
        now=datetime(2026, 9, 8, 16, 31, tzinfo=timezone.utc),
        trading_days={date(2026, 9, 9)},
    )
    market = _decision(next_day, DueKind.MARKET)
    assert market.scheduled_for == "market:2026-09-09"
    assert market.reason_code == "before_cutoff"


def test_trading_calendar_missing_and_non_trading_days_are_not_guessed():
    missing = calculate_due_work(
        now=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        trading_days=None,
    )
    assert _decision(missing, DueKind.MARKET).reason_code == "trading_calendar_missing"
    assert _decision(missing, DueKind.MARKET).due is False

    closed = calculate_due_work(
        now=datetime(2026, 9, 13, 12, tzinfo=timezone.utc),
        trading_days={date(2026, 9, 11)},
    )
    assert _decision(closed, DueKind.MARKET).reason_code == "non_trading_day"
    assert _decision(closed, DueKind.CATALOG).due is False  # 20:00 Beijing


def test_financial_requires_new_period_and_does_not_daily_refetch_history():
    values = calculate_due_work(
        now=datetime(2026, 9, 8, 13, tzinfo=timezone.utc),
        trading_days={date(2026, 9, 8)},
        new_report_periods=("2026Q2", "2025Q4"),
        completed_report_periods=("2025Q4",),
        completed_schedule_keys=("financial:2025Q4",),
    )
    financial = [item for item in values if item.kind == DueKind.FINANCIAL]
    assert [(item.payload["report_period"], item.due) for item in financial] == [
        ("2026Q2", True),
        ("2025Q4", False),
    ]

    no_signal = calculate_due_work(
        now=datetime(2026, 9, 9, 13, tzinfo=timezone.utc),
        trading_days={date(2026, 9, 9)},
    )
    assert not [item for item in no_signal if item.kind == DueKind.FINANCIAL]


def test_source_delay_keeps_expected_trade_date_and_remains_visible():
    values = calculate_due_work(
        now=datetime(2026, 9, 8, 11, 30, tzinfo=timezone.utc),
        trading_days={date(2026, 9, 8)},
        source_delayed_trade_dates=(date(2026, 9, 7),),
    )
    market = _decision(values, DueKind.MARKET)
    assert market.payload == {
        "expected_trade_date": "2026-09-08",
        "source_delayed_trade_dates": ["2026-09-07"],
    }


def test_event_overlap_includes_thirty_days_and_open_lifecycles():
    as_of = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    window = event_incremental_window(
        as_of=as_of,
        last_safe_through=as_of - timedelta(days=1),
        has_update_marker=False,
        incomplete_lifecycle_ids=("pledge:1", "buyback:2", "pledge:1"),
    )
    assert window.start == as_of - timedelta(days=30)
    assert window.end == as_of
    assert window.incomplete_lifecycle_ids == ("pledge:1", "buyback:2")
    assert window.limitation == "silent_revisions_before_overlap_are_not_guaranteed"

    late_resume = event_incremental_window(
        as_of=as_of,
        last_safe_through=as_of - timedelta(days=90),
        has_update_marker=False,
    )
    assert late_resume.start == as_of - timedelta(days=90)

    marked = event_incremental_window(
        as_of=as_of,
        last_safe_through=as_of - timedelta(hours=3),
        has_update_marker=True,
    )
    assert marked.start == as_of - timedelta(hours=3)
    assert marked.limitation is None


def test_status_reads_more_than_five_hundred_jobs_and_exhausts_partial_retries():
    class Storage:
        storage_namespace_id = "namespace-1"

        def __init__(self):
            self.job_limit = "not-called"

        @staticmethod
        def get_run_context(run_id, expected_pins=None):
            return SimpleNamespace(
                run_id=run_id,
                storage_namespace_id="namespace-1",
                ticker="600519",
            )

        def list_jobs(self, run_id, *, limit=None):
            self.job_limit = limit
            return [
                {
                    "job_id": f"job-{index:03d}",
                    "plan_item_id": f"plan-{index:03d}",
                    "dataset_id": f"dataset-{index:03d}",
                    "max_attempts": 2,
                }
                for index in range(501)
            ]

        @staticmethod
        def list_pages(job_id):
            return []

        @staticmethod
        def list_records(*, job_id=None, limit=None):
            return []

        @staticmethod
        def unprojected_snapshot_ids(job_id):
            return ()

    class Repository:
        def __init__(self):
            self.attempt_limit = "not-called"

        @staticmethod
        def get_run(run_id):
            return {
                "run_id": run_id,
                "storage_namespace_id": "namespace-1",
                "ticker": "600519",
            }

        def list_attempts(self, *, run_id, limit=None):
            self.attempt_limit = limit
            return [
                {
                    "attempt_id": f"attempt-{ordinal}",
                    "physical_query_plan_item_id": "plan-500",
                    "retry_ordinal": ordinal,
                }
                for ordinal in range(2)
            ]

        @staticmethod
        def list_attempt_events(attempt_id):
            return [{"outcome": "partial_success"}]

    storage = Storage()
    repository = Repository()
    bridge = StructuredExecutionBridge(
        repository=repository,
        storage=storage,
        source_gate=object(),
        snapshot_service=SimpleNamespace(storage_namespace_id="namespace-1"),
    )

    status = bridge.status("run-501")
    assert storage.job_limit is None
    assert repository.attempt_limit is None
    assert status.total_jobs == 501
    assert status.pending == 500
    assert status.failed == 1
    assert status.jobs[-1].job_id == "job-500"
    assert status.jobs[-1].attempt_count == 2
    assert status.jobs[-1].next_retry_ordinal is None
    assert "job-500" not in {
        item.job_id for item in bridge.resume_candidates("run-501")
    }
