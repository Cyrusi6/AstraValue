from __future__ import annotations

from datetime import date, datetime, timezone

from .models import (
    AssumptionRecord,
    ClaimKind,
    ClaimRecord,
    FactRecord,
    ReportCreateRequest,
    ScenarioName,
    SourceRecord,
    VerificationStatus,
)


DEMO_AS_OF = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


def build_demo_request() -> ReportCreateRequest:
    """构造不代表任何真实上市公司的验算夹具。"""

    official = SourceRecord(
        source_id="src-demo-official",
        name="虚构公司2025年报（测试夹具）",
        source_type="official-document-fixture",
        upstream_source_id="demo-official-filing",
        url="https://example.invalid/demo/annual-report",
        published_at=datetime(2026, 3, 30, tzinfo=timezone.utc),
        retrieved_at=DEMO_AS_OF,
        document_hash="demo-official-hash",
        authority_level=1,
        notes="仅用于软件验算，不是现实证券数据",
    )
    review = SourceRecord(
        source_id="src-demo-review",
        name="虚构行业协会统计（测试夹具）",
        source_type="industry-fixture",
        upstream_source_id="demo-independent-industry",
        url="https://example.invalid/demo/industry",
        published_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
        retrieved_at=DEMO_AS_OF,
        authority_level=2,
        notes="独立上游的虚构复核源，仅用于测试",
    )
    analyst = SourceRecord(
        source_id="src-demo-analyst",
        name="用户情景假设（测试夹具）",
        source_type="analyst-assumption-fixture",
        upstream_source_id="demo-user-assumptions",
        retrieved_at=DEMO_AS_OF,
        authority_level=5,
        notes="透明情景假设，不是披露事实",
    )
    sources = [official, review, analyst]
    facts = _demo_facts([official.source_id, review.source_id])
    fact_ids = {item.metric_id: item.fact_id for item in facts if item.period_end == date(2025, 12, 31)}
    claims = [
        _claim("claim-business", "business", "公司收入以工业设备与售后服务为主。", official.source_id),
        _claim("claim-moat", "moat", "规模化制造与长期客户认证构成可观察的交付壁垒。", review.source_id),
        _claim("claim-governance", "governance", "测试夹具未模拟监管处罚，治理结论仍需真实公告补齐。", official.source_id),
        _claim("claim-capital", "capital", "资本配置应以投入资本回报是否持续高于资本成本为检验标准。", official.source_id),
        _claim("claim-driver", "driver", "订单放量通过收入与产能利用率传导至利润和估值。", review.source_id),
        _claim("claim-risk", "risk", "原材料价格与扩产节奏错配是主要经营风险。", review.source_id),
        _claim("claim-industry", "industry", "行业竞争需结合有效产能而非只看名义产能。", review.source_id),
        _claim("claim-catalyst", "catalyst", "新产线按期爬坡可能改善单位成本。", official.source_id),
        _claim("claim-invalidation", "invalidation", "若经营现金流长期无法覆盖资本开支，增长论点失效。", official.source_id),
    ]
    assumptions = _demo_assumptions(analyst.source_id)
    assumption_ids = {(item.scenario.value, item.name): item.assumption_id for item in assumptions}
    base = ScenarioName.BASE.value
    model_inputs = {
        "VAL.DCF.FCFF": {
            "base_fcff": 1_430_000_000.0,
            "wacc": 0.095,
            "shares_outstanding": 1_000_000_000.0,
            "growth_rates": [0.10, 0.09, 0.08, 0.07, 0.05],
            "terminal_growth": 0.02,
            "net_debt": 900_000_000.0,
            "_lineage": {
                "base_fcff": [f"fact:{fact_ids['operating_cash_flow']}", f"fact:{fact_ids['capital_expenditure']}", "formula:FIN.CASHFLOW@1.0.0"],
                "wacc": f"assumption:{assumption_ids[(base, 'wacc')]}",
                "shares_outstanding": f"fact:{fact_ids['shares_outstanding']}",
                "growth_rates": "source:src-demo-analyst",
                "terminal_growth": f"assumption:{assumption_ids[(base, 'terminal_growth')]}",
                "net_debt": [f"fact:{fact_ids['interest_bearing_debt']}", f"fact:{fact_ids['cash']}", "formula:FIN.BALANCE@1.0.0"],
            },
        },
        "VAL.RELATIVE": {
            "metric_per_share": 1.1,
            "target_multiple": 15.0,
            "low_multiple": 12.0,
            "high_multiple": 18.0,
            "_lineage": {
                "metric_per_share": [f"fact:{fact_ids['net_income_excl']}", f"fact:{fact_ids['shares_outstanding']}", "formula:VAL.RELATIVE@1.0.0"],
                "target_multiple": "source:src-demo-analyst",
                "low_multiple": "source:src-demo-analyst",
                "high_multiple": "source:src-demo-analyst",
            },
        },
        "VAL.SOTP": {
            "segments": [
                {"name": "工业设备", "value": 14_000_000_000.0},
                {"name": "售后服务", "value": 3_000_000_000.0},
            ],
            "net_debt": 900_000_000.0,
            "shares_outstanding": 1_000_000_000.0,
            "_lineage": {
                "segments": "source:src-demo-analyst",
                "net_debt": [f"fact:{fact_ids['interest_bearing_debt']}", f"fact:{fact_ids['cash']}"],
                "shares_outstanding": f"fact:{fact_ids['shares_outstanding']}",
            },
        },
    }
    return ReportCreateRequest(
        ticker="000001-DEMO",
        company_name="示例智造（虚构）",
        industry="制造业",
        as_of=DEMO_AS_OF,
        current_price=13.5,
        price_as_of=DEMO_AS_OF,
        sources=sources,
        facts=facts,
        claims=claims,
        assumptions=assumptions,
        model_inputs=model_inputs,
        report_notes="全量数据均为虚构测试夹具，不可用于投资判断。",
    )


def _claim(claim_id: str, category: str, text: str, source_id: str) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        ticker="000001-DEMO",
        category=category,
        text=text,
        claim_kind=ClaimKind.DISCLOSED_FACT,
        evidence_source_ids=[source_id],
        confidence=0.85,
        as_of=DEMO_AS_OF,
    )


def _demo_facts(source_ids: list[str]) -> list[FactRecord]:
    facts: list[FactRecord] = []

    def add(metric: str, value: float, unit: str, period_end: date, period_type: str = "annual") -> None:
        facts.append(
            FactRecord(
                fact_id=f"fact-{metric}-{period_end.isoformat()}-{period_type}",
                ticker="000001-DEMO",
                metric_id=metric,
                value=value,
                unit=unit,
                period_end=period_end,
                period_type=period_type,
                disclosed_at=datetime(2026, 3, 30, tzinfo=timezone.utc),
                as_of=DEMO_AS_OF,
                audited=period_type == "annual",
                original_label=metric,
                source_ids=source_ids,
                verification_status=VerificationStatus.DUAL_SOURCE,
                metadata={"fixture": True, "notice": "虚构数据"},
            )
        )

    annuals = [
        (2021, 8_000_000_000.0, 650_000_000.0),
        (2022, 9_100_000_000.0, 760_000_000.0),
        (2023, 10_300_000_000.0, 900_000_000.0),
        (2024, 11_400_000_000.0, 1_020_000_000.0),
        (2025, 12_800_000_000.0, 1_200_000_000.0),
    ]
    for year, revenue, profit in annuals:
        add("revenue", revenue, "CNY", date(year, 12, 31))
        add("net_income", profit, "CNY", date(year, 12, 31))

    quarter_revenue = [2.4, 2.6, 2.8, 3.0, 2.7, 2.9, 3.1, 3.3, 3.0, 3.15, 3.25, 3.4]
    quarter_profit = [0.19, 0.22, 0.24, 0.25, 0.22, 0.25, 0.27, 0.28, 0.26, 0.29, 0.31, 0.34]
    quarter_ends = [
        date(year, month, day)
        for year in (2023, 2024, 2025)
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))
    ]
    for period_end, revenue, profit in zip(quarter_ends, quarter_revenue, quarter_profit):
        add("revenue", revenue * 1_000_000_000, "CNY", period_end, "single_quarter")
        add("net_income", profit * 1_000_000_000, "CNY", period_end, "single_quarter")

    latest = date(2025, 12, 31)
    latest_values = {
        "cost_of_revenue": (8_320_000_000.0, "CNY"),
        "net_income_parent": (1_150_000_000.0, "CNY"),
        "net_income_excl": (1_100_000_000.0, "CNY"),
        "operating_cash_flow": (2_200_000_000.0, "CNY"),
        "capital_expenditure": (770_000_000.0, "CNY"),
        "investing_cash_flow": (-1_400_000_000.0, "CNY"),
        "financing_cash_flow": (-300_000_000.0, "CNY"),
        "opening_cash": (2_000_000_000.0, "CNY"),
        "closing_cash": (2_520_000_000.0, "CNY"),
        "fx_effect": (20_000_000.0, "CNY"),
        "cash": (2_600_000_000.0, "CNY"),
        "accounts_receivable": (1_500_000_000.0, "CNY"),
        "inventory": (1_800_000_000.0, "CNY"),
        "accounts_payable": (1_200_000_000.0, "CNY"),
        "goodwill": (300_000_000.0, "CNY"),
        "interest_bearing_debt": (3_500_000_000.0, "CNY"),
        "current_assets": (7_200_000_000.0, "CNY"),
        "current_liabilities": (4_100_000_000.0, "CNY"),
        "total_assets": (18_000_000_000.0, "CNY"),
        "total_liabilities": (7_000_000_000.0, "CNY"),
        "total_equity": (11_000_000_000.0, "CNY"),
        "average_assets": (17_000_000_000.0, "CNY"),
        "average_equity": (10_500_000_000.0, "CNY"),
        "ebit": (1_600_000_000.0, "CNY"),
        "tax_rate": (0.25, "ratio"),
        "invested_capital": (10_000_000_000.0, "CNY"),
        "shares_outstanding": (1_000_000_000.0, "share"),
        "capacity_utilization": (0.82, "ratio"),
        "rd_expense": (640_000_000.0, "CNY"),
    }
    for metric, (value, unit) in latest_values.items():
        add(metric, value, unit, latest)
    return facts


def _demo_assumptions(source_id: str) -> list[AssumptionRecord]:
    values = {
        ScenarioName.BEAR: {
            "base_revenue": 12_800_000_000.0, "volume_growth": 0.00, "price_growth": -0.03,
            "gross_margin": 0.30, "expense_ratio": 0.20, "tax_rate": 0.25,
            "depreciation_ratio": 0.03, "capex_ratio": 0.08, "working_capital_ratio": 0.09,
            "wacc": 0.105, "terminal_growth": 0.015, "years": 5.0,
            "net_debt": 900_000_000.0, "shares_outstanding": 1_000_000_000.0,
            "minority_interest_ratio": 0.04, "non_recurring_after_tax": 20_000_000.0,
        },
        ScenarioName.BASE: {
            "base_revenue": 12_800_000_000.0, "volume_growth": 0.08, "price_growth": 0.01,
            "gross_margin": 0.35, "expense_ratio": 0.19, "tax_rate": 0.25,
            "depreciation_ratio": 0.03, "capex_ratio": 0.06, "working_capital_ratio": 0.08,
            "wacc": 0.095, "terminal_growth": 0.02, "years": 5.0,
            "net_debt": 900_000_000.0, "shares_outstanding": 1_000_000_000.0,
            "minority_interest_ratio": 0.04, "non_recurring_after_tax": 20_000_000.0,
        },
        ScenarioName.BULL: {
            "base_revenue": 12_800_000_000.0, "volume_growth": 0.15, "price_growth": 0.03,
            "gross_margin": 0.38, "expense_ratio": 0.18, "tax_rate": 0.25,
            "depreciation_ratio": 0.03, "capex_ratio": 0.05, "working_capital_ratio": 0.07,
            "wacc": 0.085, "terminal_growth": 0.025, "years": 5.0,
            "net_debt": 900_000_000.0, "shares_outstanding": 1_000_000_000.0,
            "minority_interest_ratio": 0.04, "non_recurring_after_tax": 20_000_000.0,
        },
    }
    ratios = {
        "volume_growth", "price_growth", "gross_margin", "expense_ratio", "tax_rate",
        "depreciation_ratio", "capex_ratio", "working_capital_ratio", "wacc",
        "terminal_growth", "minority_interest_ratio",
    }
    tracking = {
        "volume_growth": "销量增速", "price_growth": "平均售价", "gross_margin": "毛利率",
        "capex_ratio": "资本开支强度", "working_capital_ratio": "营运资本占用",
    }
    result = []
    for scenario, entries in values.items():
        for name, value in entries.items():
            unit = "ratio" if name in ratios else "year" if name == "years" else "CNY"
            if name == "shares_outstanding":
                unit = "share"
            result.append(
                AssumptionRecord(
                    assumption_id=f"asm-{scenario.name.lower()}-{name}",
                    scenario=scenario,
                    name=name,
                    value=value,
                    unit=unit,
                    reason="虚构夹具中的透明情景参数",
                    source_ids=[source_id],
                    valid_until=date(2027, 3, 31),
                    confirmed=False,
                    tracking_metric=tracking.get(name),
                )
            )
    return result

