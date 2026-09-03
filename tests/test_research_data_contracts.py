from datetime import date, datetime, timedelta, timezone
import sqlite3

import duckdb
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from analysis.api import create_app
from analysis.models import (
    AnnouncementRecord,
    ClaimKind,
    DimensionalFactRecord,
    DisclosureForm,
    DocumentRecord,
    EventRecord,
    EvidenceSpan,
    FactRecord,
    ForecastSnapshot,
    ForecastStatistic,
    ForecastType,
    IndustryFactRecord,
    PeerSetVersion,
    SourceRecord,
    SyncResult,
    VerificationStatus,
)
from analysis.storage import ReportStorage, StorageError
from analysis.timeseries import TimeSeriesStore


AS_OF = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


def _sources() -> tuple[SourceRecord, SourceRecord]:
    official = SourceRecord(
        source_id="source-official",
        name="正式公告",
        source_type="official-document",
        upstream_source_id="official-announcement-001",
        published_at=AS_OF - timedelta(days=2),
        authority_level=1,
    )
    independent = SourceRecord(
        source_id="source-independent",
        name="独立结构化来源",
        source_type="public-adapter",
        upstream_source_id="independent-provider",
        published_at=AS_OF - timedelta(days=1),
        authority_level=3,
    )
    return official, independent


def _records(tmp_path):
    official, independent = _sources()
    text_path = tmp_path / "announcement.txt"
    text_path.write_text("公司完成年度分红实施。", encoding="utf-8")
    document = DocumentRecord(
        document_id="document-001",
        ticker="600519",
        title="年度权益分派实施公告",
        archived_path=str(tmp_path / "announcement.pdf"),
        text_path=str(text_path),
        sha256="a" * 64,
        source=official,
        page_count=1,
    )
    supporting_fact = FactRecord(
        fact_id="fact-revenue",
        ticker="600519",
        metric_id="revenue",
        value=100.0,
        unit="CNY",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        period_type="annual",
        disclosed_at=AS_OF - timedelta(days=2),
        as_of=AS_OF - timedelta(days=2),
        source_ids=[official.source_id],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
    )
    dimensional_fact = DimensionalFactRecord(
        dimensional_fact_id="dimension-001",
        ticker="600519",
        metric_id="segment_revenue",
        dimension_type="product",
        dimension_code="product-a",
        dimension_name="产品A",
        value=80.0,
        unit="CNY",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        available_at=AS_OF - timedelta(days=1),
        source_ids=[official.source_id, independent.source_id],
        document_ids=[document.document_id],
        document_page=1,
        table_title="分产品营业收入",
        row_label="产品A",
        column_label="营业收入",
        verification_status=VerificationStatus.DUAL_SOURCE,
        supporting_fact_ids=[supporting_fact.fact_id],
        data_snapshot_id="sync-001",
        disclosed_as=DisclosureForm.EXACT,
    )
    event = EventRecord(
        event_id="event-001",
        ticker="600519",
        event_type="dividend",
        event_subtype="implementation",
        announced_at=AS_OF - timedelta(days=2),
        available_at=AS_OF - timedelta(days=2),
        effective_at=AS_OF - timedelta(days=1),
        lifecycle_state="implemented",
        summary="年度分红已经实施",
        source_ids=[official.source_id],
        document_ids=[document.document_id],
        evidence_spans=[
            EvidenceSpan(
                document_id=document.document_id,
                page=1,
                text="公司完成年度分红实施。",
            )
        ],
        claim_kind=ClaimKind.DISCLOSED_FACT,
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        data_snapshot_id="sync-001",
        metadata={
            "canonical_announcement_key": (
                "600519:2026-09-01:年度权益分派实施公告"
            )
        },
    )
    industry_fact = IndustryFactRecord(
        industry_fact_id="industry-001",
        industry_code="consumer.baijiu",
        industry_taxonomy_version="1.0.0",
        metric_id="cr3",
        value=0.55,
        unit="ratio",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        frequency="annual",
        denominator_definition="样本公司营业收入合计",
        coverage_scope="已披露样本",
        source_ids=[official.source_id],
        dataset_version="2025-v1",
        release_at=AS_OF - timedelta(days=2),
        available_at=AS_OF - timedelta(days=1),
        is_estimate=True,
        calculation_formula="前三家公司收入/样本公司收入合计",
        method_ref="STEP.INDUSTRY@1.0.0",
        component_fact_ids=[supporting_fact.fact_id],
        verification_status=VerificationStatus.ESTIMATED,
        data_snapshot_id="sync-001",
    )
    forecast = ForecastSnapshot(
        forecast_snapshot_id="forecast-001",
        ticker="600519",
        provider="公司业绩指引",
        as_of=AS_OF,
        available_at=AS_OF - timedelta(days=1),
        target_period=date(2026, 12, 31),
        metric_id="revenue",
        value=110.0,
        unit="CNY",
        statistic=ForecastStatistic.POINT,
        forecast_type=ForecastType.MANAGEMENT_GUIDANCE,
        source_ids=[official.source_id],
        report_ids=[document.document_id],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        data_snapshot_id="sync-001",
    )
    peer_set = PeerSetVersion(
        peer_set_id="peer-set-001",
        version=1,
        target_ticker="600519",
        as_of=AS_OF,
        included_tickers=["000858"],
        excluded_tickers=["600809"],
        selection_dimensions=["业务结构", "规模"],
        inclusion_reason={"000858": "同属高端白酒且收入规模可比"},
        exclusion_reason={"600809": "产品结构和收入规模差异较大"},
        industry_taxonomy_version="1.0.0",
        source_ids=[official.source_id],
        data_snapshot_id="sync-001",
    )
    announcement = AnnouncementRecord(
        announcement_record_id="announcement-001",
        ticker="600519",
        company_name="贵州茅台",
        title="年度权益分派实施公告",
        announced_at=AS_OF - timedelta(days=2),
        available_at=AS_OF - timedelta(days=2),
        provider="cninfo",
        announcement_id="cninfo-001",
        url="https://example.test/announcement.pdf",
        category="权益分派",
        canonical_key="600519:2026-09-01:年度权益分派实施公告",
        classification_version="1.0.0",
        classified_event_type="dividend",
        classified_event_subtype="implementation",
        classified_lifecycle_state="registration",
        classification_score=1100,
        source_ids=[official.source_id],
        document_id=document.document_id,
        event_ids=[event.event_id],
        data_snapshot_id="sync-001",
    )
    return (
        [official, independent],
        document,
        supporting_fact,
        dimensional_fact,
        event,
        industry_fact,
        forecast,
        peer_set,
        announcement,
    )


def test_new_contracts_are_backward_compatible_and_validate_boundaries():
    old_sync = SyncResult.model_validate(
        {"sync_result_id": "old-sync", "ticker": "600519", "provider_results": {}}
    )
    assert old_sync.dimensional_facts == []
    assert old_sync.events == []
    assert old_sync.industry_facts == []
    assert old_sync.forecast_snapshots == []
    assert old_sync.peer_sets == []
    assert old_sync.announcements == []

    event = EventRecord(
        ticker="600519",
        event_type="risk",
        announced_at=AS_OF,
        available_at=AS_OF,
        lifecycle_state="identified",
        summary="待核验风险",
        data_snapshot_id="sync-001",
    )
    assert event.root_event_id == event.event_id

    with pytest.raises(ValidationError, match="至少两个来源"):
        DimensionalFactRecord(
            ticker="600519",
            metric_id="segment_revenue",
            dimension_type="product",
            dimension_name="产品A",
            value=1,
            available_at=AS_OF,
            source_ids=["only-one-source"],
            verification_status=VerificationStatus.DUAL_SOURCE,
            data_snapshot_id="sync-001",
        )

    with pytest.raises(ValidationError, match="快照时点后"):
        ForecastSnapshot(
            ticker="600519",
            provider="provider",
            as_of=AS_OF,
            available_at=AS_OF + timedelta(seconds=1),
            target_period=date(2026, 12, 31),
            metric_id="revenue",
            forecast_type=ForecastType.PROVIDER_CONSENSUS,
            data_snapshot_id="sync-001",
        )

    with pytest.raises(ValidationError, match="缺少理由"):
        PeerSetVersion(
            target_ticker="600519",
            as_of=AS_OF,
            included_tickers=["000858"],
            industry_taxonomy_version="1.0.0",
            data_snapshot_id="sync-001",
        )


def test_storage_persists_research_records_and_returns_lineage(tmp_path):
    storage = ReportStorage(tmp_path / "analysis.db")
    (
        sources,
        document,
        supporting_fact,
        dimensional_fact,
        event,
        industry_fact,
        forecast,
        peer_set,
        announcement,
    ) = _records(tmp_path)
    result = SyncResult(
        sync_result_id="sync-001",
        ticker="600519",
        provider_results={"fixture": "ok"},
        as_of=AS_OF,
        sources=sources,
        facts=[supporting_fact],
        documents=[document],
        dimensional_facts=[dimensional_fact],
        events=[event],
        industry_facts=[industry_fact],
        forecast_snapshots=[forecast],
        peer_sets=[peer_set],
        announcements=[announcement],
    )
    storage.save_sync_result(result)

    assert storage.get_dimensional_fact("dimension-001") == dimensional_fact
    assert storage.get_event("event-001") == event
    assert storage.get_industry_fact("industry-001") == industry_fact
    assert storage.get_forecast_snapshot("forecast-001") == forecast
    assert storage.get_peer_set_version("peer-set-001", 1) == peer_set
    assert storage.get_announcement("announcement-001") == announcement

    assert storage.list_dimensional_facts("600519", dimension_type="product") == [
        dimensional_fact
    ]
    assert storage.list_dimensional_facts(
        "600519", data_snapshot_id="sync-001"
    ) == [dimensional_fact]
    assert storage.list_dimensional_facts(
        "600519", data_snapshot_id="another-snapshot"
    ) == []
    assert storage.list_dimensional_facts(
        "600519", verification_status=VerificationStatus.DUAL_SOURCE
    ) == [dimensional_fact]
    assert storage.list_events("600519", event_type="dividend") == [event]
    assert storage.list_industry_facts("consumer.baijiu", metric_id="cr3") == [
        industry_fact
    ]
    assert storage.list_forecast_snapshots("600519", metric_id="revenue") == [
        forecast
    ]
    assert storage.list_peer_set_versions("600519") == [peer_set]
    assert storage.list_announcements("600519", event_type="dividend") == [
        announcement
    ]

    before_available = AS_OF - timedelta(days=3)
    assert storage.list_dimensional_facts("600519", as_of=before_available) == []
    assert storage.list_events("600519", as_of=before_available) == []
    assert storage.list_industry_facts("consumer.baijiu", as_of=before_available) == []
    assert storage.list_forecast_snapshots("600519", as_of=before_available) == []

    dimensional_lineage = storage.dimensional_fact_lineage("dimension-001")
    assert {item["source_id"] for item in dimensional_lineage["sources"]} == {
        "source-official",
        "source-independent",
    }
    assert dimensional_lineage["documents"][0]["document_id"] == "document-001"
    assert dimensional_lineage["supporting_facts"][0]["fact_id"] == "fact-revenue"
    assert dimensional_lineage["missing_references"] == []

    assert storage.event_lineage("event-001")["missing_references"] == []
    assert storage.industry_fact_lineage("industry-001")["missing_references"] == []
    assert storage.forecast_snapshot_lineage("forecast-001")["missing_references"] == []
    assert storage.peer_set_lineage("peer-set-001", 1)["missing_references"] == []
    assert storage.announcement_lineage("announcement-001")["missing_references"] == []

    event_v2 = EventRecord.model_validate(
        {
            **event.model_dump(mode="python"),
            "event_id": "event-002",
            "root_event_id": "event-002",
            "data_snapshot_id": "sync-002",
            "metadata": {
                **event.metadata,
                "canonical_announcement_key": announcement.canonical_key,
            },
        }
    )
    announcement_v2 = AnnouncementRecord.model_validate(
        {
            **announcement.model_dump(mode="python"),
            "announcement_record_id": "announcement-002",
            "event_ids": [event_v2.event_id],
            "data_snapshot_id": "sync-002",
        }
    )
    storage.save_events([event_v2])
    storage.save_announcements([announcement_v2])
    assert storage.list_events("600519", event_type="dividend") == [event_v2]
    assert storage.list_events(
        "600519",
        event_type="dividend",
        data_snapshot_id="sync-001",
    ) == [event]
    assert storage.list_events(
        "600519",
        event_type="dividend",
        data_snapshot_id="sync-002",
    ) == [event_v2]
    assert storage.list_announcements("600519", event_type="dividend") == [
        announcement_v2
    ]
    assert storage.list_announcements(
        "600519",
        event_type="dividend",
        data_snapshot_id="sync-001",
    ) == [announcement]
    assert storage.list_announcements(
        "600519",
        event_type="dividend",
        data_snapshot_id="sync-002",
    ) == [announcement_v2]

    with sqlite3.connect(storage.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE migration_id=?",
                ("0002_research_data_contracts",),
            ).fetchone()[0]
            == 1
        )

    changed = dimensional_fact.model_copy(update={"value": 81.0})
    with pytest.raises(StorageError, match="不可覆盖"):
        storage.save_dimensional_facts([changed])


def test_schema_v4_migrates_dimensional_verification_status_to_v5(tmp_path):
    dimensional_fact = _records(tmp_path)[3]
    db_path = tmp_path / "legacy-v4.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE dimensional_facts (
                dimensional_fact_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                metric_id TEXT NOT NULL,
                dimension_type TEXT NOT NULL,
                period_end TEXT,
                available_at TEXT NOT NULL,
                data_snapshot_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            PRAGMA user_version=4;
            """
        )
        connection.execute(
            """
            INSERT INTO dimensional_facts(
                dimensional_fact_id, ticker, metric_id, dimension_type,
                period_end, available_at, data_snapshot_id, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dimensional_fact.dimensional_fact_id,
                dimensional_fact.ticker,
                dimensional_fact.metric_id,
                dimensional_fact.dimension_type,
                dimensional_fact.period_end.isoformat(),
                dimensional_fact.available_at.isoformat(),
                dimensional_fact.data_snapshot_id,
                dimensional_fact.model_dump_json(),
            ),
        )

    storage = ReportStorage(db_path)

    assert storage.get_dimensional_fact(dimensional_fact.dimensional_fact_id) == (
        dimensional_fact
    )
    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(dimensional_facts)")
        }
        assert "verification_status" in columns
        assert connection.execute(
            "SELECT verification_status FROM dimensional_facts WHERE dimensional_fact_id=?",
            (dimensional_fact.dimensional_fact_id,),
        ).fetchone()[0] == dimensional_fact.verification_status.value
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE migration_id=?",
            ("0005_dimensional_verification_status",),
        ).fetchone()[0] == 1
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(dimensional_facts)")
        }
        assert "idx_dimensional_facts_verification" in indexes


def test_storage_rejects_false_dual_source_with_same_upstream(tmp_path):
    storage = ReportStorage(tmp_path / "analysis.db")
    official, _ = _sources()
    mirror = SourceRecord(
        source_id="source-mirror",
        name="同一公告的镜像",
        source_type="official-document",
        upstream_source_id=official.upstream_source_id,
        authority_level=1,
    )
    storage.save_sources([official, mirror])
    mislabeled = DimensionalFactRecord(
        ticker="600519",
        metric_id="segment_revenue",
        dimension_type="product",
        dimension_name="产品A",
        value=80,
        unit="CNY",
        available_at=AS_OF,
        source_ids=[official.source_id, mirror.source_id],
        verification_status=VerificationStatus.DUAL_SOURCE,
        data_snapshot_id="sync-001",
    )
    with pytest.raises(StorageError, match="相同上游"):
        storage.save_dimensional_facts([mislabeled])


def test_research_record_query_and_lineage_api(service, tmp_path):
    (
        sources,
        document,
        supporting_fact,
        dimensional_fact,
        event,
        industry_fact,
        forecast,
        peer_set,
        announcement,
    ) = _records(tmp_path)
    service.storage.save_sync_result(
        SyncResult(
            sync_result_id="sync-001",
            ticker="600519",
            provider_results={"fixture": "ok"},
            as_of=AS_OF,
            sources=sources,
            facts=[supporting_fact],
            documents=[document],
            dimensional_facts=[dimensional_fact],
            events=[event],
            industry_facts=[industry_fact],
            forecast_snapshots=[forecast],
            peer_sets=[peer_set],
            announcements=[announcement],
        )
    )
    client = TestClient(create_app(service))

    dimensions = client.get(
        "/api/companies/600519/dimensions",
        params={
            "dimension_type": "product",
            "data_snapshot_id": "sync-001",
            "verification_status": "双源一致",
        },
    )
    assert dimensions.status_code == 200
    assert dimensions.json()[0]["dimensional_fact_id"] == "dimension-001"
    review_queue = client.get(
        "/api/companies/600519/dimensions/review-queue",
        params={"data_snapshot_id": "sync-001"},
    )
    assert review_queue.status_code == 200
    assert review_queue.json()["count"] == 0
    assert client.get("/api/dimensional-facts/dimension-001/lineage").status_code == 200

    events = client.get(
        "/api/companies/600519/events",
        params={"event_type": "dividend", "data_snapshot_id": "sync-001"},
    )
    assert events.status_code == 200
    assert events.json()[0]["event_id"] == "event-001"
    assert client.get("/api/events/event-001/lineage").status_code == 200

    industry = client.get(
        "/api/industries/consumer.baijiu/facts",
        params={"metric_id": "cr3"},
    )
    assert industry.status_code == 200
    assert industry.json()[0]["industry_fact_id"] == "industry-001"
    assert client.get("/api/industry-facts/industry-001/lineage").status_code == 200

    forecasts = client.get(
        "/api/forecasts/600519/snapshots",
        params={"metric_id": "revenue"},
    )
    assert forecasts.status_code == 200
    assert forecasts.json()[0]["forecast_snapshot_id"] == "forecast-001"
    assert client.get("/api/forecasts/forecast-001/lineage").status_code == 200

    peer = client.get("/api/peer-sets/peer-set-001/versions/1")
    assert peer.status_code == 200
    assert peer.json()["target_ticker"] == "600519"
    assert (
        client.get("/api/peer-sets/peer-set-001/versions/1/lineage").status_code
        == 200
    )
    announcements = client.get(
        "/api/companies/600519/announcements",
        params={"event_type": "dividend", "data_snapshot_id": "sync-001"},
    )
    assert announcements.status_code == 200
    assert announcements.json()[0]["announcement_record_id"] == "announcement-001"
    assert client.get("/api/announcements/announcement-001/lineage").status_code == 200


def test_research_records_have_duckdb_index_and_frozen_parquet_snapshot(tmp_path):
    (
        _,
        _,
        _,
        dimensional_fact,
        event,
        industry_fact,
        forecast,
        peer_set,
        _,
    ) = _records(tmp_path)
    store = TimeSeriesStore(
        tmp_path / "timeseries.duckdb",
        tmp_path / "parquet",
    )
    records = {
        "dimensional_facts": [dimensional_fact],
        "events": [event],
        "industry_facts": [industry_fact],
        "forecast_snapshots": [forecast],
        "peer_sets": [peer_set],
    }
    store.append_research_records(**records)

    company_rows = store.query_research_records("600519")
    assert {row["record_type"] for row in company_rows} == {
        "dimensional_fact",
        "event",
        "forecast_snapshot",
        "peer_set",
    }
    assert store.query_research_records(
        "600519",
        record_type="dimensional_fact",
        metric_id="segment_revenue",
    )[0]["record_id"] == "dimension-001"
    assert store.query_research_records(
        "consumer.baijiu",
        record_type="industry_fact",
    )[0]["record_id"] == "industry-001"

    snapshot_path = store.export_research_snapshot("sync-001", **records)
    assert snapshot_path.exists()
    with duckdb.connect() as connection:
        rows = connection.execute(
            "SELECT record_type, record_id FROM read_parquet(?) ORDER BY record_type",
            [str(snapshot_path)],
        ).fetchall()
    assert len(rows) == 5
    assert {row[0] for row in rows} == {
        "dimensional_fact",
        "event",
        "forecast_snapshot",
        "industry_fact",
        "peer_set",
    }
