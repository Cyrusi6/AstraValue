from __future__ import annotations

import math
import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .models import FactRecord


class CalculationError(ValueError):
    pass


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def normalize_amount(value: float, from_unit: str, to_unit: str = "CNY") -> float:
    factors = {
        "CNY": 1.0,
        "元": 1.0,
        "千元": 1_000.0,
        "万元": 10_000.0,
        "百万元": 1_000_000.0,
        "亿元": 100_000_000.0,
    }
    if from_unit not in factors or to_unit not in factors:
        raise CalculationError(f"不支持的单位转换: {from_unit} -> {to_unit}")
    return value * factors[from_unit] / factors[to_unit]


def single_quarter(current_ytd: float, previous_ytd: float | None, quarter: int) -> float:
    if quarter not in {1, 2, 3, 4}:
        raise CalculationError("quarter必须为1至4")
    if quarter == 1:
        return current_ytd
    if previous_ytd is None:
        raise CalculationError("Q2至Q4累计值转单季必须提供上期累计值")
    return current_ytd - previous_ytd


def cagr(begin: float, end: float, years: float) -> float | None:
    if begin <= 0 or end < 0 or years <= 0:
        return None
    return (end / begin) ** (1 / years) - 1


def gross_margin(revenue: float, cost_of_revenue: float) -> float | None:
    return safe_div(revenue - cost_of_revenue, revenue)


def free_cash_flow(operating_cash_flow: float, capital_expenditure: float) -> float:
    return operating_cash_flow - abs(capital_expenditure)


def income_metrics(values: dict[str, float]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    revenue = values.get("revenue")
    if revenue is None:
        return result
    if "cost_of_revenue" in values:
        result["gross_margin"] = gross_margin(revenue, values["cost_of_revenue"])
    for source, target in (
        ("net_income", "net_margin"),
        ("net_income_parent", "parent_net_margin"),
        ("net_income_excl", "adjusted_net_margin"),
        ("selling_expense", "selling_expense_ratio"),
        ("administrative_expense", "administrative_expense_ratio"),
        ("rd_expense", "rd_ratio"),
        ("finance_expense", "finance_expense_ratio"),
    ):
        if source in values:
            result[target] = safe_div(values[source], revenue)
    return result


def balance_metrics(values: dict[str, float]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    if {"current_assets", "current_liabilities"} <= values.keys():
        result["current_ratio"] = safe_div(values["current_assets"], values["current_liabilities"])
    if {"cash", "interest_bearing_debt"} <= values.keys():
        result["net_debt"] = values["interest_bearing_debt"] - values["cash"]
    if {"interest_bearing_debt", "total_assets"} <= values.keys():
        result["interest_bearing_debt_ratio"] = safe_div(values["interest_bearing_debt"], values["total_assets"])
    if {"goodwill", "total_equity"} <= values.keys():
        result["goodwill_to_equity"] = safe_div(values["goodwill"], values["total_equity"])
    if {"accounts_receivable", "revenue"} <= values.keys():
        result["receivable_to_revenue"] = safe_div(values["accounts_receivable"], values["revenue"])
    if {"inventory", "cost_of_revenue"} <= values.keys():
        result["inventory_to_cost"] = safe_div(values["inventory"], values["cost_of_revenue"])
    return result


def cashflow_metrics(values: dict[str, float]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    operating = values.get("operating_cash_flow")
    if operating is None:
        return result
    if "capital_expenditure" in values:
        result["free_cash_flow"] = free_cash_flow(operating, values["capital_expenditure"])
    if "net_income" in values:
        result["cash_profit_ratio"] = safe_div(operating, values["net_income"])
    if "revenue" in values:
        result["operating_cash_flow_margin"] = safe_div(operating, values["revenue"])
        if "capital_expenditure" in values:
            result["capex_to_revenue"] = safe_div(abs(values["capital_expenditure"]), values["revenue"])
    return result


def working_capital_metrics(values: dict[str, float]) -> dict[str, float | None]:
    current_assets = values.get("current_assets")
    current_liabilities = values.get("current_liabilities")
    result: dict[str, float | None] = {}
    if current_assets is not None and current_liabilities is not None:
        result["net_working_capital"] = current_assets - current_liabilities
    if {"accounts_receivable", "inventory", "accounts_payable"} <= values.keys():
        result["operating_working_capital"] = (
            values["accounts_receivable"] + values["inventory"] - values["accounts_payable"]
        )
    return result


def earnings_quality_metrics(values: dict[str, float]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    if {"net_income", "operating_cash_flow", "average_assets"} <= values.keys():
        result["accrual_ratio"] = safe_div(
            values["net_income"] - values["operating_cash_flow"], values["average_assets"]
        )
    if {"net_income_excl", "net_income_parent"} <= values.keys():
        result["adjusted_profit_share"] = safe_div(values["net_income_excl"], values["net_income_parent"])
    if {"asset_impairment_loss", "revenue"} <= values.keys():
        result["impairment_to_revenue"] = safe_div(values["asset_impairment_loss"], values["revenue"])
    return result


def normalize_financial_facts(facts: list["FactRecord"]) -> list["FactRecord"]:
    """选择截至披露时点的最新重述版本，并保持原始事实不变。"""

    chosen: dict[tuple, "FactRecord"] = {}
    for fact in facts:
        key = (fact.ticker, fact.metric_id, fact.period_start, fact.period_end, fact.period_type, fact.scope)
        previous = chosen.get(key)
        rank = (
            1 if fact.is_restated else 0,
            fact.disclosed_at or fact.as_of,
            fact.as_of,
            fact.restatement_version or "",
        )
        previous_rank = None
        if previous is not None:
            previous_rank = (
                1 if previous.is_restated else 0,
                previous.disclosed_at or previous.as_of,
                previous.as_of,
                previous.restatement_version or "",
            )
        if previous is None or rank > previous_rank:
            chosen[key] = fact
    normalized = []
    metric_units = _metric_units()
    for fact in chosen.values():
        target_unit = metric_units.get(fact.metric_id)
        value = fact.value
        unit = fact.unit
        metadata = dict(fact.metadata)
        if value is not None and target_unit:
            if unit in {"元", "千元", "万元", "百万元", "亿元", "CNY"} and target_unit == "CNY":
                converted = normalize_amount(value, unit, "CNY")
                if unit != "CNY":
                    metadata.update({"original_value": value, "original_unit": unit, "unit_normalized": True})
                value, unit = converted, "CNY"
            elif unit == "%" and target_unit == "ratio":
                metadata.update({"original_value": value, "original_unit": unit, "unit_normalized": True})
                value, unit = value / 100, "ratio"
            elif unit in {"元/股", "CNY/share"} and target_unit == "CNY/share":
                unit = "CNY/share"
            elif unit == "元" and target_unit == "CNY/share":
                unit = "CNY/share"
            elif unit == "x" and target_unit == "multiple":
                unit = "multiple"
        normalized.append(fact.model_copy(update={"value": value, "unit": unit, "metadata": metadata}))
    return normalized


@lru_cache(maxsize=1)
def _metric_units() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / "config" / "methods" / "metrics.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {key: item["unit"] for key, item in data["metrics"].items()}


def dupont(net_income: float, revenue: float, average_assets: float, average_equity: float) -> dict[str, float | None]:
    net_margin = safe_div(net_income, revenue)
    asset_turnover = safe_div(revenue, average_assets)
    equity_multiplier = safe_div(average_assets, average_equity)
    roe = None
    if None not in (net_margin, asset_turnover, equity_multiplier):
        roe = net_margin * asset_turnover * equity_multiplier
    return {
        "net_margin": net_margin,
        "asset_turnover": asset_turnover,
        "equity_multiplier": equity_multiplier,
        "roe": roe,
    }


def roic(ebit: float, tax_rate: float, invested_capital: float) -> float | None:
    if not 0 <= tax_rate <= 1:
        raise CalculationError("tax_rate必须在0和1之间")
    return safe_div(ebit * (1 - tax_rate), invested_capital)


def wacc(
    equity_value: float,
    debt_value: float,
    cost_of_equity: float,
    pre_tax_cost_of_debt: float,
    tax_rate: float,
) -> float:
    capital = equity_value + debt_value
    if capital <= 0:
        raise CalculationError("总资本必须大于0")
    if not 0 <= tax_rate <= 1:
        raise CalculationError("tax_rate必须在0和1之间")
    return (
        equity_value / capital * cost_of_equity
        + debt_value / capital * pre_tax_cost_of_debt * (1 - tax_rate)
    )


@dataclass(frozen=True)
class ReconciliationResult:
    passed: bool
    difference: float
    tolerance: float
    message: str


def reconcile_balance_sheet(assets: float, liabilities: float, equity: float, tolerance: float | None = None) -> ReconciliationResult:
    tolerance = tolerance if tolerance is not None else max(abs(assets) * 0.001, 1.0)
    difference = assets - liabilities - equity
    passed = abs(difference) <= tolerance
    return ReconciliationResult(passed, difference, tolerance, "资产=负债+权益" if passed else "资产负债恒等式不平")


def reconcile_cash_flow(
    opening_cash: float,
    operating_cash_flow: float,
    investing_cash_flow: float,
    financing_cash_flow: float,
    fx_effect: float,
    closing_cash: float,
    tolerance: float | None = None,
) -> ReconciliationResult:
    expected = opening_cash + operating_cash_flow + investing_cash_flow + financing_cash_flow + fx_effect
    tolerance = tolerance if tolerance is not None else max(abs(closing_cash) * 0.001, 1.0)
    difference = expected - closing_cash
    passed = abs(difference) <= tolerance
    return ReconciliationResult(passed, difference, tolerance, "现金流勾稽一致" if passed else "现金流勾稽不一致")


def point_in_time_percentile(
    current_value: float,
    observations: Iterable[tuple[datetime, float]],
    as_of: datetime,
) -> float | None:
    values = [value for available_at, value in observations if available_at <= as_of and math.isfinite(value)]
    if not values:
        return None
    return sum(1 for value in values if value <= current_value) / len(values)


def compute_financial_snapshot(values: dict[str, float]) -> dict[str, float | None]:
    result: dict[str, float | None] = {
        **income_metrics(values),
        **balance_metrics(values),
        **cashflow_metrics(values),
        **working_capital_metrics(values),
        **earnings_quality_metrics(values),
    }
    if {"net_income", "revenue", "average_assets", "average_equity"} <= values.keys():
        result.update({f"dupont_{key}": value for key, value in dupont(
            values["net_income"], values["revenue"], values["average_assets"], values["average_equity"]
        ).items()})
    if {"ebit", "tax_rate", "invested_capital"} <= values.keys():
        result["roic"] = roic(values["ebit"], values["tax_rate"], values["invested_capital"])
    return result
