from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from .models import AssumptionRecord, ScenarioName


REQUIRED_SCENARIO_INPUTS = {
    "base_revenue",
    "volume_growth",
    "price_growth",
    "gross_margin",
    "expense_ratio",
    "tax_rate",
    "depreciation_ratio",
    "capex_ratio",
    "working_capital_ratio",
    "wacc",
    "terminal_growth",
    "years",
    "net_debt",
    "shares_outstanding",
    "minority_interest_ratio",
    "non_recurring_after_tax",
}


def build_scenario_projections(
    assumptions: list[AssumptionRecord],
    method_ref: str = "STEP.SCENARIOS@1.0.0",
    as_of: date | datetime | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[ScenarioName, dict[str, AssumptionRecord]] = defaultdict(dict)
    for assumption in assumptions:
        grouped[assumption.scenario][assumption.name] = assumption

    results = []
    cutoff = as_of.date() if isinstance(as_of, datetime) else as_of
    for scenario in (ScenarioName.BEAR, ScenarioName.BASE, ScenarioName.BULL):
        entries = grouped.get(scenario, {})
        missing = sorted(REQUIRED_SCENARIO_INPUTS - entries.keys())
        if missing:
            results.append(
                {
                    "scenario": scenario.value,
                    "status": "暂无该数据",
                    "missing": missing,
                    "method_ref": method_ref,
                }
            )
            continue
        expired = sorted(
            name for name, item in entries.items() if cutoff and item.valid_until and item.valid_until < cutoff
        )
        if expired:
            results.append(
                {
                    "scenario": scenario.value,
                    "status": "待核验",
                    "failure_reason": f"假设已过有效期: {', '.join(expired)}",
                    "method_ref": method_ref,
                    "assumption_lineage": {
                        name: f"assumption:{item.assumption_id}" for name, item in entries.items()
                    },
                }
            )
            continue
        values = {name: item.value for name, item in entries.items()}
        try:
            result = _calculate_scenario(values)
            result.update(
                {
                    "scenario": scenario.value,
                    "status": "已计算",
                    "confirmed": all(item.confirmed for item in entries.values()),
                    "unconfirmed": sorted(name for name, item in entries.items() if not item.confirmed),
                    "method_ref": method_ref,
                    "assumption_lineage": {
                        name: f"assumption:{item.assumption_id}" for name, item in entries.items()
                    },
                }
            )
            results.append(result)
        except ValueError as exc:
            results.append(
                {
                    "scenario": scenario.value,
                    "status": "待核验",
                    "failure_reason": str(exc),
                    "method_ref": method_ref,
                    "assumption_lineage": {
                        name: f"assumption:{item.assumption_id}" for name, item in entries.items()
                    },
                }
            )
    return results


def _calculate_scenario(values: dict[str, float]) -> dict[str, Any]:
    years = int(values["years"])
    if years < 1 or years > 20:
        raise ValueError("years必须在1至20之间")
    for name in (
        "gross_margin",
        "expense_ratio",
        "tax_rate",
        "depreciation_ratio",
        "capex_ratio",
        "working_capital_ratio",
        "minority_interest_ratio",
    ):
        if not 0 <= values[name] <= 1:
            raise ValueError(f"{name}必须在0和1之间")
    if values["wacc"] <= values["terminal_growth"]:
        raise ValueError("WACC必须大于永续增长率")
    if values["shares_outstanding"] <= 0 or values["base_revenue"] <= 0:
        raise ValueError("收入和股本必须大于0")

    revenue = values["base_revenue"]
    revenue_growth = (1 + values["volume_growth"]) * (1 + values["price_growth"]) - 1
    projections = []
    cashflows = []
    previous_revenue = revenue
    for year in range(1, years + 1):
        revenue = previous_revenue * (1 + revenue_growth)
        gross_profit = revenue * values["gross_margin"]
        operating_expense = revenue * values["expense_ratio"]
        ebit = gross_profit - operating_expense
        nopat = ebit * (1 - values["tax_rate"])
        depreciation = revenue * values["depreciation_ratio"]
        capex = revenue * values["capex_ratio"]
        change_in_working_capital = (revenue - previous_revenue) * values["working_capital_ratio"]
        fcff = nopat + depreciation - capex - change_in_working_capital
        net_income = nopat
        adjusted_parent_net_income = (
            net_income * (1 - values["minority_interest_ratio"])
            - values["non_recurring_after_tax"]
        )
        eps = adjusted_parent_net_income / values["shares_outstanding"]
        projections.append(
            {
                "year": year,
                "revenue": round(revenue, 2),
                "ebit": round(ebit, 2),
                "net_income": round(net_income, 2),
                "net_income_excl_parent": round(adjusted_parent_net_income, 2),
                "eps": round(eps, 4),
                "fcff": round(fcff, 2),
            }
        )
        cashflows.append(fcff)
        previous_revenue = revenue

    if cashflows[-1] <= 0:
        raise ValueError("末期FCFF非正，DCF不适用")
    discount_rate = values["wacc"]
    terminal_growth = values["terminal_growth"]
    pv = sum(cashflow / (1 + discount_rate) ** year for year, cashflow in enumerate(cashflows, 1))
    terminal = cashflows[-1] * (1 + terminal_growth) / (discount_rate - terminal_growth)
    enterprise_value = pv + terminal / (1 + discount_rate) ** years
    equity_value = enterprise_value - values["net_debt"]
    fair_value = equity_value / values["shares_outstanding"]
    if fair_value <= 0:
        raise ValueError("情景估值得到非正权益价值")
    return {
        "revenue_growth": round(revenue_growth, 6),
        "projections": projections,
        "fair_value_per_share": round(fair_value, 4),
    }
