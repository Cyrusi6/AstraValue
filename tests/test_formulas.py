from datetime import date, datetime, timezone

import pytest

from analysis.formulas import (
    CalculationError,
    balance_metrics,
    cashflow_metrics,
    dupont,
    earnings_quality_metrics,
    income_metrics,
    normalize_amount,
    normalize_financial_facts,
    point_in_time_percentile,
    reconcile_balance_sheet,
    reconcile_cash_flow,
    roic,
    single_quarter,
    wacc,
    working_capital_metrics,
)
from analysis.models import FactRecord


def test_unit_conversion_and_single_quarter():
    assert normalize_amount(1, "亿元", "万元") == 10_000
    assert single_quarter(100, None, 1) == 100
    assert single_quarter(250, 100, 2) == 150
    with pytest.raises(CalculationError):
        single_quarter(250, None, 2)


def test_normalize_financial_facts_prefers_latest_restatement():
    common = dict(
        ticker="000001",
        metric_id="revenue",
        unit="CNY",
        period_end=date(2025, 12, 31),
        period_type="annual",
    )
    old = FactRecord(
        **common,
        fact_id="old",
        value=100,
        disclosed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    restated = FactRecord(
        **common,
        fact_id="new",
        value=110,
        disclosed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        as_of=datetime(2026, 5, 1, tzinfo=timezone.utc),
        is_restated=True,
        restatement_version="2",
    )
    assert normalize_financial_facts([old, restated])[0].fact_id == "new"


def test_financial_metric_blocks():
    values = {
        "revenue": 1_000,
        "cost_of_revenue": 600,
        "net_income": 100,
        "net_income_parent": 95,
        "net_income_excl": 90,
        "operating_cash_flow": 160,
        "capital_expenditure": 50,
        "current_assets": 500,
        "current_liabilities": 250,
        "cash": 120,
        "interest_bearing_debt": 200,
        "total_assets": 1_400,
        "total_equity": 700,
        "accounts_receivable": 100,
        "inventory": 80,
        "accounts_payable": 60,
        "average_assets": 1_300,
        "asset_impairment_loss": 5,
    }
    assert income_metrics(values)["gross_margin"] == pytest.approx(0.4)
    assert balance_metrics(values)["current_ratio"] == 2
    assert cashflow_metrics(values)["free_cash_flow"] == 110
    assert working_capital_metrics(values)["operating_working_capital"] == 120
    assert earnings_quality_metrics(values)["adjusted_profit_share"] == pytest.approx(90 / 95)


def test_dupont_and_roic_wacc():
    result = dupont(100, 1_000, 500, 250)
    assert result["roe"] == pytest.approx(0.4)
    assert roic(100, 0.25, 500) == pytest.approx(0.15)
    assert wacc(800, 200, 0.1, 0.05, 0.25) == pytest.approx(0.0875)


def test_reconciliations_and_point_in_time():
    assert reconcile_balance_sheet(1_000, 600, 400).passed
    assert reconcile_cash_flow(100, 50, -20, -10, 0, 120).passed
    cutoff = datetime(2025, 1, 2, tzinfo=timezone.utc)
    observations = [
        (datetime(2025, 1, 1, tzinfo=timezone.utc), 10),
        (datetime(2025, 1, 3, tzinfo=timezone.utc), 100),
    ]
    assert point_in_time_percentile(10, observations, cutoff) == 1

