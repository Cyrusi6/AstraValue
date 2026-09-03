from datetime import datetime, timezone

import pytest

from analysis.dimensional_parser import parse_dimensional_facts
from analysis.models import (
    DisclosureForm,
    DocumentRecord,
    SourceRecord,
    VerificationStatus,
)


PUBLISHED_AT = datetime(2026, 4, 17, tzinfo=timezone.utc)


def _document(tmp_path, text: str, *, title: str = "测试公司2025年年度报告"):
    text_path = tmp_path / "annual.pdf.txt"
    text_path.write_text(text, encoding="utf-8")
    return DocumentRecord(
        document_id="document-annual",
        ticker="600000",
        title=title,
        archived_path=str(tmp_path / "annual.pdf"),
        text_path=str(text_path),
        sha256="a" * 64,
        source=SourceRecord(
            source_id="official-annual",
            name="交易所正式披露",
            source_type="official-document",
            upstream_source_id="official-document:fixture",
            published_at=PUBLISHED_AT,
            authority_level=1,
        ),
        page_count=10,
    )


def _complete_annual_text(*, product_revenue: str = "200 100") -> str:
    return f"""
--- page 8 ---
(1). 主营业务分行业、分产品、分地区、分销售模式情况
单位：万元 币种：人民币
主营业务分行业情况
分行业
营业收入
营业成本
毛利率（%）
营业收入比上年增减（%）
营业成本比上年增减（%）
毛利率比上年增减（%）
酒类
300 100 66.67 10 8 增加1个百分点
主营业务分产品情况
分产品
营业收入 营业成本 毛利率（%）
核心产品
{product_revenue.split()[0]} 50 75 10 8 1
其他系列
酒
{product_revenue.split()[1]} 50 50 10 8 1
主营业务分地区情况
分地区
营业收入 营业成本 毛利率（%）
国内
240 80 66.67 10 8 1
国外
60 20 66.67 10 8 1
主营业务分销售模式情况
销售模式
营业收入 营业成本 毛利率（%）
批发代理
180 60 66.67 10 8 1
直销
120 40 66.67 10 8 1
(2). 产销量情况分析表
主要产品
单位
生产量 销售量 库存量
库存量比上年增
减（%）
酒类
GWh
40 35 10 5 4 3
(3). 重大采购合同、重大销售合同的履行情况
前五名客户销售额120万元，占年度销售总额40%；其中前五名客户销售额中关联方销售额0万元。
前五名供应商采购额80万元，占年度采购总额20%。
--- page 9 ---
现有产能
主要工厂名称
设计产能
实际产能
一号工厂
50 45
说明：计量单位为GWh，按报告期实际产量计算。
"""


def _find(facts, metric_id: str, dimension_type: str, dimension_name: str):
    return next(
        item
        for item in facts
        if item.metric_id == metric_id
        and item.dimension_type == dimension_type
        and item.dimension_name == dimension_name
    )


def test_parses_split_labels_multi_value_lines_and_evidence(tmp_path):
    document = _document(tmp_path, _complete_annual_text())
    facts = parse_dimensional_facts(document, data_snapshot_id="snapshot-a")

    split_label = _find(facts, "segment_revenue", "product", "其他系列酒")
    production = _find(facts, "production_volume", "product", "酒类")
    capacity = _find(facts, "actual_capacity", "capacity", "一号工厂")

    assert split_label.value == pytest.approx(1_000_000)
    assert production.value == pytest.approx(40)
    assert production.unit == "GWh"
    assert capacity.value == pytest.approx(45)
    assert capacity.unit == "GWh"
    assert split_label.verification_status == VerificationStatus.AUTHORITATIVE_SINGLE
    assert split_label.evidence_spans[0].page == 8
    assert split_label.evidence_spans[0].table_title == "主营业务分产品情况"
    assert split_label.evidence_spans[0].row_label == "其他系列酒"
    assert split_label.evidence_spans[0].column_label == "营业收入"
    assert split_label.evidence_spans[0].start_offset is not None
    assert split_label.evidence_spans[0].end_offset > split_label.evidence_spans[0].start_offset


def test_reconciles_totals_and_gross_margin(tmp_path):
    facts = parse_dimensional_facts(
        _document(tmp_path, _complete_annual_text()),
        data_snapshot_id="snapshot-a",
    )

    margin = _find(facts, "segment_margin", "business", "酒类")
    product_revenue = _find(facts, "segment_revenue", "product", "核心产品")

    assert margin.metadata["gross_margin_check"]["status"] == "consistent"
    assert product_revenue.metadata["table_reconciliation"]["status"] == "consistent"
    assert product_revenue.verification_status == VerificationStatus.AUTHORITATIVE_SINGLE


def test_conflicts_are_downgraded_instead_of_averaged(tmp_path):
    text = _complete_annual_text(product_revenue="190 100").replace(
        "300 100 66.67 10 8 增加1个百分点",
        "300 100 50 10 8 增加1个百分点",
    )
    facts = parse_dimensional_facts(
        _document(tmp_path, text),
        data_snapshot_id="snapshot-a",
    )

    margin = _find(facts, "segment_margin", "business", "酒类")
    product_revenue = _find(facts, "segment_revenue", "product", "核心产品")

    assert margin.verification_status == VerificationStatus.PENDING
    assert margin.metadata["gross_margin_check"]["status"] == "conflict"
    assert product_revenue.verification_status == VerificationStatus.PENDING
    assert product_revenue.metadata["table_reconciliation"]["status"] == "conflict"
    assert product_revenue.metadata["table_reconciliation"]["group_sum"] == pytest.approx(
        2_900_000
    )


def test_top_five_aggregates_keep_anonymized_disclosure_form(tmp_path):
    facts = parse_dimensional_facts(
        _document(tmp_path, _complete_annual_text()),
        data_snapshot_id="snapshot-a",
    )

    amount = _find(facts, "top5_customer_sales", "customer", "前五名客户合计")
    ratio = _find(facts, "customer_concentration", "customer", "前五名客户合计")

    assert amount.value == pytest.approx(1_200_000)
    assert amount.disclosed_as == DisclosureForm.ANONYMIZED
    assert ratio.value == pytest.approx(0.4)
    assert ratio.disclosed_as == DisclosureForm.PERCENTAGE_ONLY
    assert amount.evidence_spans[0].page == 8


def test_non_annual_report_is_ignored_and_same_snapshot_is_deterministic(tmp_path):
    document = _document(tmp_path, _complete_annual_text())
    first = parse_dimensional_facts(document, data_snapshot_id="snapshot-a")
    second = parse_dimensional_facts(document, data_snapshot_id="snapshot-a")
    quarterly = _document(
        tmp_path,
        _complete_annual_text(),
        title="测试公司2025年第三季度报告",
    )

    assert first == second
    assert parse_dimensional_facts(quarterly, data_snapshot_id="snapshot-a") == []


def test_szse_partial_segments_gwh_operations_and_split_concentration(tmp_path):
    text = """
--- page 20 ---
（2）占公司营业收入或营业利润10%以上的行业、产品、地区、销售模式的情况
单位：千元
项目
营业收入
营业成本
毛利率
营业收入比上年同期增减
营业成本比上年同期增减
毛利率比上年同期增减
分业务
电气机械及器材
制造业
1,000 700 30% 10% 8% 1%
分产品
动力电池系统
600 400 33.33% 10% 8% 1%
分地区
境内
800 550 31.25% 10% 8% 1%
公司主营业务数据统计口径在报告期发生调整
5）不同产品或业务的产销情况
项目
产能
在建产能
产能利用率
产量
电池系统（GWh）
100 20 90% 90
（3）公司实物销售收入是否大于劳务收入
行业分类
项目
单位
2025 年
2024 年
同比增减
电池系统
销售量
GWh
80
70
14.29%
生产量
GWh
90
75
20%
--- page 21 ---
库存量
GWh
15
10
50%
相关数据同比发生变动30%以上的原因说明
前五名客户合计销售金额（千元）
400
前五名客户合计销售金额占年度销售总额比例
40%
前五名供应商合计采购金额（千元）
200
前五名供应商合计采购金额占年度采购总额比例
20%
"""
    facts = parse_dimensional_facts(
        _document(tmp_path, text),
        data_snapshot_id="snapshot-szse",
    )

    business = _find(
        facts,
        "segment_revenue",
        "business",
        "电气机械及器材制造业",
    )
    product = _find(facts, "segment_revenue", "product", "动力电池系统")
    capacity = _find(facts, "production_capacity", "capacity", "电池系统")
    utilization = _find(facts, "capacity_utilization", "capacity", "电池系统")
    inventory = _find(facts, "ending_inventory_volume", "product", "电池系统")
    customer = _find(
        facts,
        "top5_customer_sales",
        "customer",
        "前五名客户合计",
    )

    assert business.value == pytest.approx(1_000_000)
    assert product.value == pytest.approx(600_000)
    assert product.metadata["reconciliation_scope"] == "partial"
    assert product.verification_status == VerificationStatus.AUTHORITATIVE_SINGLE
    assert capacity.value == pytest.approx(100)
    assert capacity.unit == "GWh"
    assert utilization.value == pytest.approx(0.9)
    assert inventory.value == pytest.approx(15)
    assert inventory.unit == "GWh"
    assert inventory.evidence_spans[0].page == 21
    assert customer.value == pytest.approx(400_000)
    assert customer.disclosed_as == DisclosureForm.ANONYMIZED
