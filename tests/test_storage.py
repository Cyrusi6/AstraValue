from copy import deepcopy
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from analysis.models import (
    AssumptionPatchRequest,
    DimensionalFactRecord,
    EvidenceSpan,
    EventRecord,
    ResearchRating,
    ReviewRequest,
    SyncResult,
    VerificationStatus,
)
from analysis.storage import StorageError


def test_report_versions_are_immutable_and_recalculation_is_pinned(service, demo_request):
    first = service.create_report(demo_request)
    with pytest.raises(StorageError):
        service.storage.save_report(first)
    second = service.recalculate(first.report_id)
    assert second.version == first.version + 1
    assert second.parent_report_id == first.report_id
    assert second.method_bundle_id == first.method_bundle_id
    assert second.data_snapshot_id == first.data_snapshot_id
    assert service.storage.get_report(first.report_id).model_dump(mode="json") == first.model_dump(mode="json")


def test_report_derivations_preserve_and_persist_dimensional_facts(service, demo_request):
    official = demo_request.sources[0]
    dimension = DimensionalFactRecord(
        dimensional_fact_id="dimension-version-freeze",
        ticker=demo_request.ticker,
        metric_id="segment_revenue",
        dimension_type="product",
        dimension_name="工业设备",
        value=8_000_000_000.0,
        unit="CNY",
        period_end=demo_request.facts[0].period_end,
        period_type="annual",
        available_at=demo_request.as_of,
        source_ids=[official.source_id],
        document_ids=["document-version-freeze"],
        evidence_spans=[
            EvidenceSpan(
                document_id="document-version-freeze",
                page=12,
                text="工业设备收入为80亿元。",
            )
        ],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        data_snapshot_id="dimension-sync-version-freeze",
    )
    demo_request.dimensional_facts = [dimension]
    demo_request.dimensional_sync_result_id = "dimension-sync-version-freeze"
    event = EventRecord(
        event_id="event-version-freeze",
        ticker=demo_request.ticker,
        event_type="repurchase",
        event_subtype="plan",
        announced_at=demo_request.as_of,
        available_at=demo_request.as_of,
        lifecycle_state="plan",
        summary="披露股份回购计划",
        source_ids=[official.source_id],
        document_ids=["document-event-version-freeze"],
        evidence_spans=[
            EvidenceSpan(
                document_id="document-event-version-freeze",
                page=3,
                text="公司披露股份回购计划。",
            )
        ],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        data_snapshot_id="event-sync-version-freeze",
    )
    demo_request.events = [event]
    demo_request.event_sync_result_id = "event-sync-version-freeze"
    first = service.builder.build(demo_request)
    service.storage.save_report(first)

    recalculated = service.recalculate(first.report_id)
    reanalyzed = service.reanalyze(recalculated.report_id)
    reviewed = service.review(
        reanalyzed.report_id,
        ReviewRequest(rating=ResearchRating.UNRATED, confirm_assumptions=True),
    )

    for report in (recalculated, reanalyzed, reviewed):
        assert report.dimensional_facts == [dimension]
        assert report.audit.dimensional_fact_ids == [dimension.dimensional_fact_id]
        assert report.events == [event]
        assert report.audit.event_ids == [event.event_id]
        assert report.request_metadata["dimensional_sync_result_id"] == (
            "dimension-sync-version-freeze"
        )
        assert report.data_snapshot_id == first.data_snapshot_id
        assert report.request_metadata["event_sync_result_id"] == (
            "event-sync-version-freeze"
        )
    assert service.storage.get_dimensional_fact(dimension.dimensional_fact_id) == dimension
    assert service.storage.get_event(event.event_id) == event
    rows = service.timeseries.query_research_records(
        demo_request.ticker,
        record_type="dimensional_fact",
    )
    assert [row["record_id"] for row in rows] == [dimension.dimensional_fact_id]
    event_rows = service.timeseries.query_research_records(
        demo_request.ticker,
        record_type="event",
    )
    assert [row["record_id"] for row in event_rows] == [event.event_id]


def test_patch_without_recalculate_removes_stale_results(service, demo_report):
    assumptions = deepcopy(demo_report.assumptions)
    assumptions[0].value += 1
    patched = service.patch_assumptions(demo_report.report_id, AssumptionPatchRequest(assumptions=assumptions, recalculate=False))
    assert patched.request_metadata["calculations_stale"] is True
    assert patched.sections[7].tables == []
    recalculated = service.recalculate(patched.report_id)
    assert recalculated.sections[7].tables


def test_review_rebuilds_and_confirms_unrated_report(service, demo_report):
    reviewed = service.review(demo_report.report_id, ReviewRequest(rating=ResearchRating.UNRATED, confirm_assumptions=True))
    assert reviewed.status.value == "已复核"
    assert reviewed.conclusion.rating_confirmed
    assert all(item.confirmed for item in reviewed.assumptions)


def test_timeseries_snapshot_created(service, demo_report):
    path = service.timeseries.parquet_root / demo_report.ticker / f"{demo_report.data_snapshot_id}.parquet"
    assert path.exists()
    assert len(service.timeseries.query(demo_report.ticker)) == len(demo_report.facts)


def test_parquet_snapshot_contains_only_point_in_time_report_facts(service, demo_request):
    future = deepcopy(demo_request.facts[0])
    future.fact_id = "future-fact-must-not-enter-snapshot"
    future.as_of = demo_request.as_of + timedelta(days=1)
    future.disclosed_at = demo_request.as_of + timedelta(days=1)
    demo_request.facts.append(future)

    report = service.create_report(demo_request)
    path = service.timeseries.parquet_root / report.ticker / f"{report.data_snapshot_id}.parquet"
    with duckdb.connect() as connection:
        rows = connection.execute(
            "SELECT fact_id FROM read_parquet(?) ORDER BY fact_id",
            [str(path)],
        ).fetchall()
    snapshot_ids = {row[0] for row in rows}
    assert snapshot_ids == {item.fact_id for item in report.facts}
    assert future.fact_id not in snapshot_ids


def test_latest_financial_sync_ignores_newer_announcement_only_batch(service):
    as_of = datetime(2026, 9, 3, tzinfo=timezone.utc)
    financial = SyncResult(
        sync_result_id="financial-sync",
        ticker="600519",
        scopes=["financials", "market"],
        provider_results={"fixture": "financial"},
        as_of=as_of,
        created_at=as_of,
    )
    announcements = SyncResult(
        sync_result_id="announcement-sync",
        ticker="600519",
        scopes=["announcements"],
        provider_results={"fixture": "announcements"},
        as_of=as_of,
        created_at=as_of + timedelta(minutes=1),
    )
    service.storage.save_sync_result(financial)
    service.storage.save_sync_result(announcements)

    assert service.storage.latest_sync_result("600519").sync_result_id == (
        "announcement-sync"
    )
    assert service.storage.latest_sync_result(
        "600519",
        required_scope="financials",
    ).sync_result_id == "financial-sync"
