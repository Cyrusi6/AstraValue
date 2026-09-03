from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest

from analysis.adapters.akshare_adapter import _financial_rows, _statement_rows
from analysis.adapters.baostock_adapter import _query_profit_facts
from analysis.adapters.official_adapter import (
    FilingCandidate,
    _candidate_attempts,
    _download_pdf,
)
from analysis.adapters.sina_adapter import (
    _emit_sina_facts,
    _sina_market_fact,
    _sina_statement_rows,
)
from analysis.adapters.tushare_adapter import TushareProAdapter
from analysis.filing_parser import FilingPeriod, derive_single_quarter_facts, parse_official_document
from analysis.models import (
    DimensionalFactRecord,
    DocumentRecord,
    EventRecord,
    FactRecord,
    ReportCreateRequest,
    SourceRecord,
    SyncResult,
    VerificationStatus,
)
from analysis.verification import consolidate_facts, verify_pair


class FakeRow(dict):
    @property
    def index(self):
        return self.keys()


class _FakeIloc:
    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, index):
        return self.rows[index]


class FakeFrame:
    def __init__(self, rows):
        self.rows = [FakeRow(item) for item in rows]
        self.columns = sorted({key for row in self.rows for key in row})
        self.empty = not self.rows
        self.iloc = _FakeIloc(self.rows)

    def iterrows(self):
        yield from enumerate(self.rows)


def test_akshare_period_selection_uses_latest_five_years_and_supports_twelve_quarters():
    rows = []
    for year in range(2019, 2027):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            if year == 2026 and month > 6:
                continue
            rows.append({"报告期": f"{year:04d}{month:02d}{day:02d}", "营业收入": year * 10 + month})
    # AKShare currently returns newest-first; reverse order verifies that the
    # selector does not depend on iloc[0].
    frame = FakeFrame(list(reversed(rows)))
    selected = _financial_rows(frame, date(2026, 9, 3), annual_years=5, single_quarters=12)
    annual = [item for item, _ in selected if (item.month, item.day) == (12, 31)]
    assert annual == [date(year, 12, 31) for year in range(2021, 2026)]

    source = SourceRecord(source_id="ak", name="AK", upstream_source_id="eastmoney")
    cumulative = []
    for report_date, row in selected:
        annual_period = (report_date.month, report_date.day) == (12, 31)
        cumulative.append(
            FactRecord(
                ticker="600519",
                metric_id="revenue",
                value=float(row["营业收入"]),
                unit="元",
                period_start=date(report_date.year, 1, 1),
                period_end=report_date,
                period_type="annual" if annual_period else "cumulative",
                source_ids=[source.source_id],
            )
        )
    derived = derive_single_quarter_facts(cumulative, 12)
    single_periods = sorted(
        item.period_end for item in derived if item.period_type == "single_quarter"
    )
    assert len(single_periods) == 12
    assert single_periods[-1] == date(2026, 6, 30)


def test_akshare_statement_rows_reject_future_disclosure_and_future_update():
    frame = FakeFrame(
        [
            {
                "REPORT_DATE": "2025-12-31",
                "NOTICE_DATE": "2026-04-17",
                "UPDATE_DATE": "2026-04-17",
            },
            {
                "REPORT_DATE": "2026-03-31",
                "NOTICE_DATE": "2026-04-25",
                "UPDATE_DATE": "2026-08-01",
            },
        ]
    )
    rows = _statement_rows(frame, date(2026, 5, 1), annual_years=5, single_quarters=12)
    assert [item[0] for item in rows] == [date(2025, 12, 31)]


def test_official_parser_skips_note_number_and_separates_parent_equity(tmp_path):
    text_path = tmp_path / "report.pdf.txt"
    text_path.write_text(
        """--- page 1 ---
主要会计数据和财务指标
单位：元
营业收入
100,000.00
90,000.00
归属于上市公司股东的净利润
20,000.00
18,000.00
归属于上市公司股东的扣除非经常性损益的净利润
19,000.00
17,000.00
经营活动产生的现金流量净额
30,000.00
25,000.00
总资产
500,000.00
450,000.00
归属于上市公司股东的净资产
300,000.00
280,000.00
非经常性损益项目
--- page 2 ---
合并资产负债表
编制单位：测试公司
单位：元
项目
附注
2025年12月31日
2024年12月31日
货币资金
1
120,000.00
110,000.00
资产总计
500,000.00
450,000.00
负债合计
180,000.00
160,000.00
所有者权益（或股东权益）合计
320,000.00
290,000.00
合并利润表
""",
        encoding="utf-8",
    )
    source = SourceRecord(
        name="巨潮资讯正式披露",
        source_type="official-document",
        upstream_source_id="official-document:fixture",
        published_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        authority_level=1,
    )
    document = DocumentRecord(
        ticker="000001",
        title="测试公司2025年年度报告",
        archived_path=str(tmp_path / "report.pdf"),
        text_path=str(text_path),
        sha256="fixture",
        source=source,
        page_count=2,
    )
    facts = parse_official_document(document)
    values = {(item.metric_id, item.period_type, item.period_end): item for item in facts}
    current = date(2025, 12, 31)
    prior = date(2024, 12, 31)
    assert values[("cash", "instant", current)].value == 120_000
    assert values[("cash", "instant", current)].document_page == 2
    assert values[("cash", "instant", prior)].value == 110_000
    assert values[("cash", "instant", prior)].is_restated
    assert values[("total_parent_equity", "instant", current)].value == 300_000
    assert values[("total_equity", "instant", current)].value == 320_000


def test_official_parser_recognizes_prefixed_opening_cash_label(tmp_path):
    text_path = tmp_path / "cashflow.pdf.txt"
    text_path.write_text(
        """--- page 1 ---
合并现金流量表
编制单位：测试公司
单位：元
项目
2025年度
2024年度
五、现金及现金等价物净增加额
10,000.00
8,000.00
加：期初现金及现金等价物余额
56（2）
50,000.00 42,000.00
六、期末现金及现金等价物余额
60,000.00 50,000.00
母公司现金流量表
""",
        encoding="utf-8",
    )
    source = SourceRecord(
        name="巨潮资讯正式披露",
        source_type="official-document",
        upstream_source_id="official-document:cashflow-fixture",
        published_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        authority_level=1,
    )
    document = DocumentRecord(
        ticker="000001",
        title="测试公司2025年年度报告",
        archived_path=str(tmp_path / "cashflow.pdf"),
        text_path=str(text_path),
        sha256="cashflow-fixture",
        source=source,
        page_count=1,
    )

    facts = parse_official_document(document)
    opening = next(item for item in facts if item.metric_id == "opening_cash")
    closing = next(item for item in facts if item.metric_id == "closing_cash")
    assert opening.value == 50_000
    assert opening.period_type == "instant"
    assert closing.value == 60_000


def test_sina_rows_respect_update_date_and_support_single_quarter_derivation():
    frame = FakeFrame(
        [
            {
                "报告日": f"2025-{month:02d}-{day:02d}",
                "公告日期": f"2025-{notice_month:02d}-20",
                "更新日期": f"2025-{notice_month:02d}-21",
                "类型": "合并",
                "归属于母公司所有者的净利润": value,
            }
            for month, day, notice_month, value in (
                (3, 31, 4, 10.0),
                (6, 30, 8, 30.0),
                (9, 30, 10, 60.0),
                (12, 31, 12, 100.0),
            )
        ]
        + [
            {
                "报告日": "2026-03-31",
                "公告日期": "2026-04-20",
                "更新日期": "2026-05-02",
                "类型": "合并",
                "归属于母公司所有者的净利润": 12.0,
            },
            {
                "报告日": "2025-12-31",
                "公告日期": "2026-01-01",
                "更新日期": "2026-01-01",
                "类型": "母公司",
                "归属于母公司所有者的净利润": 999.0,
            },
        ]
    )
    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = _sina_statement_rows(frame, cutoff, annual_years=5, single_quarters=12)
    assert [item[0] for item in rows] == [
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    ]
    source = SourceRecord(
        source_id="sina",
        name="新浪",
        source_type="public-adapter",
        upstream_source_id="sina-financial-statements",
        authority_level=4,
    )
    emitted = _emit_sina_facts(
        "600519",
        rows,
        {"归属于母公司所有者的净利润": ("net_income_parent", "元")},
        "income",
        source,
    )
    derived = derive_single_quarter_facts(emitted, 12)
    q3 = next(
        item
        for item in derived
        if item.period_type == "single_quarter"
        and item.period_end == date(2025, 9, 30)
    )
    assert q3.value == 30
    assert all(item.is_restated for item in emitted)


def test_sina_market_selects_latest_non_future_unadjusted_close():
    frame = FakeFrame(
        [
            {"date": "2026-09-01", "close": "1299.56"},
            {"date": "2026-09-02", "close": "1297.50"},
            {"date": "2026-09-03", "close": "1300.00"},
        ]
    )
    source = SourceRecord(
        source_id="sina-market",
        name="新浪行情",
        source_type="public-adapter",
        upstream_source_id="sina-market",
        authority_level=4,
    )
    fact = _sina_market_fact(
        frame,
        "600519",
        date(2026, 9, 2),
        source,
    )
    assert fact.value == 1297.5
    assert fact.period_end == date(2026, 9, 2)
    assert fact.metadata["adjust"] == "none"


class _FakeBaoQuery:
    error_code = "0"
    error_msg = ""
    fields = ["statDate", "pubDate", "netProfit"]

    def __init__(self, rows):
        self.rows = rows
        self.index = -1

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


class _FakeBaoStock:
    def __init__(self):
        self.calls = []

    def query_profit_data(self, *, code, year, quarter):
        self.calls.append((code, year, quarter))
        rows = {
            (2023, 2): [["2023-06-30", "2023-08-03", "37331971189.28"]],
            (2023, 3): [["2023-09-30", "2023-10-21", "54827171371.97"]],
        }.get((year, quarter), [])
        return _FakeBaoQuery(rows)


def test_baostock_quarter_profit_connects_and_derives_single_quarter():
    fake = _FakeBaoStock()
    source = SourceRecord(
        source_id="baostock-profit",
        name="BaoStock盈利",
        source_type="public-adapter",
        upstream_source_id="baostock-financial-profit",
        authority_level=4,
    )
    cutoff = datetime(2024, 1, 1, tzinfo=timezone.utc)
    facts = _query_profit_facts(
        fake,
        "sh.600519",
        "600519",
        cutoff,
        cutoff.date(),
        annual_years=1,
        single_quarters=4,
        source=source,
    )
    derived = derive_single_quarter_facts(facts, 4)
    q3 = next(
        item
        for item in derived
        if item.period_type == "single_quarter"
        and item.period_end == date(2023, 9, 30)
    )
    assert q3.value == pytest.approx(17_495_200_182.69)
    assert ("sh.600519", 2023, 3) in fake.calls


def test_tushare_without_token_is_explicitly_unavailable(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="未配置TUSHARE_TOKEN"):
        TushareProAdapter().sync("600519")


def test_html_challenge_is_not_accepted_as_official_pdf():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"<html>javascript challenge</html>", headers={"content-type": "text/html"})
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(RuntimeError, match="不是PDF"):
            _download_pdf(client, "https://example.test/report.pdf")


def test_exchange_download_candidate_has_cninfo_fallback():
    period = FilingPeriod(2025, "annual", date(2025, 12, 31))
    published = datetime(2026, 4, 1, tzinfo=timezone.utc)
    sse = FilingCandidate(
        ticker="600519",
        company_name="贵州茅台",
        title="贵州茅台2025年年度报告",
        published_at=published,
        url="https://www.sse.com.cn/report.pdf",
        provider="sse",
        announcement_id="sse-id",
        period=period,
        metadata={},
    )
    cninfo = FilingCandidate(
        ticker="600519",
        company_name="贵州茅台",
        title=sse.title,
        published_at=published,
        url="https://static.cninfo.com.cn/report.pdf",
        provider="cninfo",
        announcement_id="cn-id",
        period=period,
        metadata={},
    )
    assert [item.provider for item in _candidate_attempts(sse, [sse, cninfo])] == [
        "sse",
        "cninfo",
    ]


def test_critical_fact_requires_official_plus_independent_source():
    official = SourceRecord(
        source_id="official",
        name="正式PDF",
        source_type="official-document",
        upstream_source_id="official-document:same-hash",
        authority_level=1,
    )
    mirror = SourceRecord(
        source_id="mirror",
        name="交易所同份PDF",
        source_type="official-document",
        upstream_source_id="official-document:same-hash",
        authority_level=1,
    )
    independent = SourceRecord(
        source_id="ak",
        name="AKShare东方财富",
        source_type="public-adapter",
        upstream_source_id="eastmoney-financial-statements",
        authority_level=4,
    )

    def fact(fact_id, value, source_id):
        return FactRecord(
            fact_id=fact_id,
            ticker="600519",
            metric_id="revenue",
            value=value,
            unit="元",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            period_type="annual",
            as_of=datetime(2026, 4, 1, tzinfo=timezone.utc),
            source_ids=[source_id],
        )

    duplicate = verify_pair(
        fact("official-fact", 100, "official"),
        fact("mirror-fact", 100, "mirror"),
        {"official": official, "mirror": mirror},
    )
    assert duplicate.status == VerificationStatus.AUTHORITATIVE_SINGLE

    verified = verify_pair(
        fact("official-fact", 100, "official"),
        fact("ak-fact", 100.05, "ak"),
        {"official": official, "ak": independent},
    )
    assert verified.status == VerificationStatus.DUAL_SOURCE
    conflict = verify_pair(
        fact("official-fact", 100, "official"),
        fact("ak-conflict", 120, "ak"),
        {"official": official, "ak": independent},
    )
    assert conflict.status == VerificationStatus.PENDING
    assert conflict.accepted_value is None


def test_saved_sync_batch_automatically_supplies_report_facts(service):
    published = datetime(2026, 4, 1, tzinfo=timezone.utc)
    official = SourceRecord(
        source_id="official",
        name="正式PDF",
        source_type="official-document",
        upstream_source_id="official-document:hash",
        published_at=published,
        authority_level=1,
    )
    independent = SourceRecord(
        source_id="ak",
        name="AKShare",
        source_type="public-adapter",
        upstream_source_id="eastmoney",
        authority_level=4,
    )
    raw = [
        FactRecord(
            fact_id=f"revenue-{source.source_id}",
            ticker="600519",
            metric_id="revenue",
            value=value,
            unit="元",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            period_type="annual",
            disclosed_at=published,
            as_of=published,
            source_ids=[source.source_id],
        )
        for source, value in ((official, 100.0), (independent, 100.05))
    ]
    facts, records = consolidate_facts(raw, [official, independent])
    dimension = DimensionalFactRecord(
        dimensional_fact_id="dimension-product-revenue",
        ticker="600519",
        metric_id="segment_revenue",
        dimension_type="product",
        dimension_name="茅台酒",
        value=80.0,
        unit="元",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        period_type="annual",
        available_at=published,
        source_ids=[official.source_id],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        data_snapshot_id="sync-with-dimensions",
    )
    sync = SyncResult(
        sync_result_id="financial-sync-for-report",
        ticker="600519",
        company_name="贵州茅台",
        scopes=["financials"],
        as_of=datetime(2026, 5, 1, tzinfo=timezone.utc),
        provider_results={"official": "ok", "akshare": "ok"},
        sources=[official, independent],
        facts=facts,
        verification_records=records,
    )
    service.storage.save_sync_result(sync)
    dimension_sync = SyncResult(
        sync_result_id="sync-with-dimensions",
        ticker="600519",
        company_name="贵州茅台",
        scopes=["dimensions"],
        as_of=datetime(2026, 5, 1, tzinfo=timezone.utc),
        provider_results={"official": "dimensions ok"},
        sources=[official],
        dimensional_facts=[dimension],
    )
    service.storage.save_sync_result(dimension_sync)
    event = EventRecord(
        event_id="event-dividend-report",
        ticker="600519",
        event_type="dividend",
        event_subtype="implementation",
        announced_at=published,
        available_at=published,
        lifecycle_state="implemented",
        summary="2025年度权益分派已实施",
        amount=10.0,
        currency="CNY/share",
        source_ids=[official.source_id],
        document_ids=["document-dividend-report"],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        data_snapshot_id="sync-with-events",
    )
    event_sync = SyncResult(
        sync_result_id="sync-with-events",
        ticker="600519",
        company_name="贵州茅台",
        scopes=["announcements"],
        as_of=datetime(2026, 5, 1, tzinfo=timezone.utc),
        provider_results={"official": "events ok"},
        sources=[official],
        events=[event],
    )
    service.storage.save_sync_result(event_sync)
    report = service.create_report(
        ReportCreateRequest(
            ticker="600519",
            company_name="贵州茅台",
            industry="普通工商",
            as_of=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )
    )
    assert report.request_metadata["sync_result_id"] == sync.sync_result_id
    assert report.request_metadata["dimensional_sync_result_id"] == (
        dimension_sync.sync_result_id
    )
    assert report.request_metadata["event_sync_result_id"] == event_sync.sync_result_id
    assert report.facts[0].verification_status == VerificationStatus.DUAL_SOURCE
    assert report.dimensional_facts == [dimension]
    assert report.audit.dimensional_fact_ids == [dimension.dimensional_fact_id]
    assert report.events == [event]
    assert report.audit.event_ids == [event.event_id]
    assert report.sections[0].tables[0]["name"] == "经营维度事实"
    assert report.sections[0].tables[0]["rows"][0]["lineage"] == (
        "dimensional_fact:dimension-product-revenue"
    )
    assert report.sections[3].tables[0]["name"] == "公司事件与状态链"
    assert report.sections[3].tables[0]["rows"][0]["lineage"] == (
        "event:event-dividend-report"
    )
    assert report.sections[5].tables[0]["name"] == "公司事件与状态链"
    assert report.audit.verification_records
    assert service.storage.get_sync_result(sync.sync_result_id).ticker == "600519"
    lineage = service.storage.fact_lineage(report.facts[0].fact_id)
    assert lineage["verifications"]
