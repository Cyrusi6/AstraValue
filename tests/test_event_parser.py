from datetime import datetime, timezone

import pytest

from analysis.event_parser import enrich_event_from_document
from analysis.models import (
    DocumentRecord,
    EventRecord,
    EvidenceSpan,
    SourceRecord,
    VerificationStatus,
)


PUBLISHED_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _document(tmp_path, text: str) -> DocumentRecord:
    text_path = tmp_path / "event.pdf.txt"
    text_path.write_text(text, encoding="utf-8")
    source = SourceRecord(
        source_id="official-source",
        name="正式公告",
        source_type="official-document",
        upstream_source_id="official-document:fixture",
        published_at=PUBLISHED_AT,
        authority_level=1,
    )
    return DocumentRecord(
        document_id="document-event",
        ticker="600519",
        title="测试事件公告",
        archived_path=str(tmp_path / "event.pdf"),
        text_path=str(text_path),
        sha256="b" * 64,
        source=source,
        page_count=2,
    )


def _event(
    event_type: str,
    document: DocumentRecord,
    *,
    lifecycle_state: str | None = None,
    event_subtype: str | None = None,
) -> EventRecord:
    return EventRecord(
        event_id=f"event-{event_type}",
        ticker="600519",
        event_type=event_type,
        announced_at=PUBLISHED_AT,
        available_at=PUBLISHED_AT,
        event_subtype=event_subtype,
        lifecycle_state=lifecycle_state
        or {
            "dividend": "registration",
            "repurchase": "completed",
            "management_change": "effective",
        }.get(event_type, "announced"),
        summary=document.title,
        source_ids=[document.source.source_id],
        document_ids=[document.document_id],
        evidence_spans=[
            EvidenceSpan(document_id=document.document_id, text=document.title)
        ],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        status_updated_at=PUBLISHED_AT,
        data_snapshot_id="sync-event",
    )


def test_dividend_fields_are_normalized_and_keep_page_evidence(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
本次利润分配以总股本为基数，每10股派发现金红利238.80元。
共计派发现金红利300.01亿元。
股权登记日：2026年6月26日，除权除息日：2026年6月29日，
现金红利发放日：2026年6月29日。
""",
    )
    enriched = enrich_event_from_document(_event("dividend", document), document)

    assert enriched.amount == pytest.approx(30_001_000_000)
    assert enriched.event_terms["total_cash_dividend"] == pytest.approx(
        30_001_000_000
    )
    assert enriched.event_terms["cash_dividend_per_share"] == pytest.approx(23.88)
    assert enriched.event_terms["record_date"] == "2026-06-26"
    assert enriched.event_terms["ex_dividend_date"] == "2026-06-29"
    assert enriched.event_terms["payment_date"] == "2026-06-29"
    assert all(
        span.page == 1
        for span in enriched.evidence_spans
        if span.start_offset is not None
    )


def test_repurchase_amount_shares_ratio_and_prices_are_deterministic(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
回购方案说明。
--- page 2 ---
截至本公告日，公司已累计回购股份471.89万股，占公司总股本的比例为0.3756%。
已支付的资金总额为60.01亿元，最高成交价为1,500.00元/股，
最低成交价为1,200.00元/股。
""",
    )
    enriched = enrich_event_from_document(_event("repurchase", document), document)

    assert enriched.amount == pytest.approx(6_001_000_000)
    assert enriched.shares == pytest.approx(4_718_900)
    assert enriched.ratio == pytest.approx(0.003756)
    assert enriched.event_terms["highest_repurchase_price"] == pytest.approx(1500)
    assert enriched.event_terms["lowest_repurchase_price"] == pytest.approx(1200)
    extracted = set(enriched.metadata["extracted_fields"])
    assert {
        "repurchase_amount",
        "repurchased_shares",
        "repurchased_share_ratio",
        "highest_repurchase_price",
        "lowest_repurchase_price",
    } <= extracted
    assert any(
        span.page == 2
        for span in enriched.evidence_spans
        if span.start_offset is not None
    )


def test_repurchase_plan_keeps_plan_bounds_without_historical_actuals(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
本次回购金额不低于人民币30亿元且不超过人民币60亿元，
回购价格不超过1,600.00元/股。
公司上一期已累计回购股份218.86万股，已支付资金总额29.99亿元。
""",
    )
    event = _event(
        "repurchase",
        document,
        lifecycle_state="plan",
        event_subtype="plan",
    )

    enriched = enrich_event_from_document(event, document)

    assert enriched.amount is None
    assert enriched.shares is None
    assert enriched.ratio is None
    assert enriched.event_terms["planned_repurchase_amount_min"] == pytest.approx(
        3_000_000_000
    )
    assert enriched.event_terms["planned_repurchase_amount_max"] == pytest.approx(
        6_000_000_000
    )
    assert enriched.event_terms["repurchase_price_cap"] == pytest.approx(1600)
    assert "repurchase_amount" not in enriched.event_terms
    assert "repurchased_shares" not in enriched.event_terms


def test_repurchase_report_table_keeps_plan_range_without_actuals(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
回购股份金额：不低于人民币15亿元（含）且不超过人民币30
亿元（含）。
预计回购金额
人民币15亿元（含）—人民币30亿元（含）
回购价格上限
1,863.67元/股（含）
上一期累计已回购股数
2,188,614股
""",
    )
    event = _event(
        "repurchase",
        document,
        lifecycle_state="in_progress",
        event_subtype="plan_document",
    )

    enriched = enrich_event_from_document(event, document)

    assert enriched.amount is None
    assert enriched.shares is None
    assert enriched.ratio is None
    assert enriched.event_terms["planned_repurchase_amount_min"] == pytest.approx(
        1_500_000_000
    )
    assert enriched.event_terms["planned_repurchase_amount_max"] == pytest.approx(
        3_000_000_000
    )
    assert enriched.event_terms["repurchase_price_cap"] == pytest.approx(1863.67)


def test_repurchase_progress_prefers_cumulative_table_over_monthly_values(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
累计已回购股数
822,200股
累计已回购股数占总
股本比例
0.0655%
累计已回购金额
1,199,883,179.92元
实际回购价格区间
1,417.01元/股～1,507.41元/股
--- page 2 ---
2025年2月，公司累计回购股份137,100股，占公司总股本的比例为0.0109%，
支付的金额为199,973,608.79元。截至2025年2月底，公司已累计回购股份
822,200股，占公司总股本的比例为0.0655%，已支付的总金额为
1,199,883,179.92元。
""",
    )

    enriched = enrich_event_from_document(_event("repurchase", document), document)

    assert enriched.shares == pytest.approx(822_200)
    assert enriched.ratio == pytest.approx(0.000655)
    assert enriched.amount == pytest.approx(1_199_883_179.92)
    assert enriched.event_terms["lowest_repurchase_price"] == pytest.approx(1417.01)
    assert enriched.event_terms["highest_repurchase_price"] == pytest.approx(1507.41)


def test_holding_change_result_extracts_completed_share_count(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
截至2025年12月26日，本次增持计划已实施完毕。茅台集团增持了
2,071,359股公司股票，占公司总股本的0.17%，增持金额为
3,000,089,293.91元。
""",
    )

    enriched = enrich_event_from_document(
        _event("holding_change", document, lifecycle_state="completed"),
        document,
    )

    assert enriched.shares == pytest.approx(2_071_359)
    assert enriched.amount == pytest.approx(3_000_089_293.91)


def test_management_person_and_position_are_captured_as_parties(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
经董事会审议通过，同意聘任张三先生为公司董事会秘书，任期三年。
""",
    )
    enriched = enrich_event_from_document(
        _event("management_change", document),
        document,
    )
    assert enriched.parties == [
        {"person_name": "张三", "position": "董事会秘书"}
    ]


def test_unrelated_numbers_are_not_promoted_to_event_amounts(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
公司第五届董事会共有7名董事，本次会议于2026年6月1日召开。
""",
    )
    enriched = enrich_event_from_document(
        _event("management_change", document),
        document,
    )
    assert enriched.amount is None
    assert enriched.shares is None
    assert enriched.ratio is None


def test_dividend_parser_handles_exchange_table_layout_and_split_words(tmp_path):
    document = _document(
        tmp_path,
        """
--- page 1 ---
每股分配比例
A 股每股现金红利28.02423元
相关日期
股份类别
股权登记日
最后交易日
除权（息）
日
现金红利发放日
Ａ股
2026/6/25
－
2026/6/26
2026/6/26
--- page 2 ---
本次利润分配以公司总股本为基数，每股派发现金红利28.02423元，共计派
发现金红利35,032,574,305.19元。
""",
    )
    enriched = enrich_event_from_document(_event("dividend", document), document)

    assert enriched.amount == pytest.approx(35_032_574_305.19)
    assert enriched.event_terms["cash_dividend_per_share"] == pytest.approx(
        28.02423
    )
    assert enriched.event_terms["record_date"] == "2026-06-25"
    assert enriched.event_terms["ex_dividend_date"] == "2026-06-26"
    assert enriched.event_terms["payment_date"] == "2026-06-26"
