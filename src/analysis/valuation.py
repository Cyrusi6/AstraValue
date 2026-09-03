from __future__ import annotations

import math
from typing import Any, Callable

from .models import ModelRunStatus, ValuationResult


class ValuationError(ValueError):
    pass


def _positive(inputs: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if inputs.get(name) is None]
    if missing:
        raise ValuationError(f"缺少输入: {', '.join(missing)}")
    invalid = [name for name in names if float(inputs[name]) <= 0]
    if invalid:
        raise ValuationError(f"输入必须大于0: {', '.join(invalid)}")


def _success(method_ref: str, fair: float, inputs: dict[str, Any], low: float | None = None, high: float | None = None, **kwargs) -> ValuationResult:
    if not math.isfinite(fair) or fair <= 0:
        return ValuationResult(
            method_ref=method_ref,
            status=ModelRunStatus.FAILED,
            inputs=inputs,
            failure_reason="模型得到非正或非有限价值，不输出目标价",
        )
    low_value = low if low is not None else fair
    high_value = high if high is not None else fair
    low_value, high_value = min(low_value, high_value), max(low_value, high_value)
    return ValuationResult(
        method_ref=method_ref,
        status=ModelRunStatus.SUCCESS,
        fair_value_per_share=round(fair, 4),
        range_low=round(low_value, 4),
        range_high=round(high_value, 4),
        inputs=inputs,
        **kwargs,
    )


def relative_valuation(inputs: dict[str, Any], method_ref: str = "VAL.RELATIVE@1.0.0") -> ValuationResult:
    _positive(inputs, "metric_per_share", "target_multiple")
    metric = float(inputs["metric_per_share"])
    multiple = float(inputs["target_multiple"])
    low_multiple = float(inputs.get("low_multiple", multiple))
    high_multiple = float(inputs.get("high_multiple", multiple))
    if low_multiple <= 0 or high_multiple <= 0:
        raise ValuationError("估值倍数区间必须为正")
    return _success(method_ref, metric * multiple, inputs, metric * low_multiple, metric * high_multiple)


def _project_cashflows(base: float, growth_rates: list[float]) -> list[float]:
    if not growth_rates:
        raise ValuationError("至少需要一个显式预测期")
    values = []
    current = base
    for rate in growth_rates:
        if rate <= -1:
            raise ValuationError("增长率不能小于等于-100%")
        current *= 1 + rate
        values.append(current)
    return values


def _dcf_value(cashflows: list[float], discount_rate: float, terminal_growth: float) -> float:
    if discount_rate <= terminal_growth:
        raise ValuationError("折现率必须大于永续增长率")
    if discount_rate <= 0:
        raise ValuationError("折现率必须为正")
    if not cashflows or cashflows[-1] <= 0:
        raise ValuationError("末期现金流必须为正")
    pv = sum(value / (1 + discount_rate) ** year for year, value in enumerate(cashflows, 1))
    terminal = cashflows[-1] * (1 + terminal_growth) / (discount_rate - terminal_growth)
    return pv + terminal / (1 + discount_rate) ** len(cashflows)


def fcff_dcf(inputs: dict[str, Any], method_ref: str = "VAL.DCF.FCFF@1.0.0") -> ValuationResult:
    _positive(inputs, "base_fcff", "wacc", "shares_outstanding")
    base = float(inputs["base_fcff"])
    wacc = float(inputs["wacc"])
    terminal_growth = float(inputs.get("terminal_growth", 0.02))
    rates = [float(item) for item in inputs.get("growth_rates", [0.08] * 5)]
    cashflows = _project_cashflows(base, rates)
    enterprise_value = _dcf_value(cashflows, wacc, terminal_growth)
    equity_value = enterprise_value - float(inputs.get("net_debt", 0))
    shares = float(inputs["shares_outstanding"])
    fair = equity_value / shares
    sensitivity: dict[str, float | None] = {}
    values = []
    for delta_wacc in (-0.01, 0, 0.01):
        for delta_g in (-0.005, 0, 0.005):
            key = f"wacc={wacc + delta_wacc:.3f},g={terminal_growth + delta_g:.3f}"
            try:
                ev = _dcf_value(cashflows, wacc + delta_wacc, terminal_growth + delta_g)
                value = (ev - float(inputs.get("net_debt", 0))) / shares
                sensitivity[key] = round(value, 4) if value > 0 else None
                if value > 0:
                    values.append(value)
            except ValuationError:
                sensitivity[key] = None
    return _success(method_ref, fair, inputs, min(values) if values else fair, max(values) if values else fair, sensitivity=sensitivity)


def fcfe_dcf(inputs: dict[str, Any], method_ref: str = "VAL.DCF.FCFE@1.0.0") -> ValuationResult:
    _positive(inputs, "base_fcfe", "cost_of_equity", "shares_outstanding")
    rates = [float(item) for item in inputs.get("growth_rates", [0.08] * 5)]
    cashflows = _project_cashflows(float(inputs["base_fcfe"]), rates)
    equity_value = _dcf_value(cashflows, float(inputs["cost_of_equity"]), float(inputs.get("terminal_growth", 0.02)))
    fair = equity_value / float(inputs["shares_outstanding"])
    return _success(method_ref, fair, inputs)


def ddm(inputs: dict[str, Any], method_ref: str = "VAL.DDM@1.0.0") -> ValuationResult:
    _positive(inputs, "dividend_per_share", "cost_of_equity")
    rates = [float(item) for item in inputs.get("growth_rates", [0.05] * 5)]
    dividends = _project_cashflows(float(inputs["dividend_per_share"]), rates)
    value = _dcf_value(dividends, float(inputs["cost_of_equity"]), float(inputs.get("terminal_growth", 0.02)))
    return _success(method_ref, value, inputs)


def residual_income(inputs: dict[str, Any], method_ref: str = "VAL.RESIDUAL_INCOME@1.0.0") -> ValuationResult:
    _positive(inputs, "book_value_per_share", "roe", "cost_of_equity")
    book = float(inputs["book_value_per_share"])
    roe_value = float(inputs["roe"])
    cost = float(inputs["cost_of_equity"])
    payout = float(inputs.get("payout_ratio", 0.3))
    years = int(inputs.get("years", 5))
    terminal_growth = float(inputs.get("terminal_growth", 0.02))
    if not 0 <= payout <= 1:
        raise ValuationError("payout_ratio必须在0和1之间")
    pv_residual = 0.0
    current_book = book
    final_residual = 0.0
    for year in range(1, years + 1):
        earnings = current_book * roe_value
        final_residual = earnings - current_book * cost
        pv_residual += final_residual / (1 + cost) ** year
        current_book += earnings * (1 - payout)
    if cost <= terminal_growth:
        raise ValuationError("权益成本必须大于永续增长率")
    terminal = final_residual * (1 + terminal_growth) / (cost - terminal_growth)
    fair = book + pv_residual + terminal / (1 + cost) ** years
    return _success(method_ref, fair, inputs)


def sotp(inputs: dict[str, Any], method_ref: str = "VAL.SOTP@1.0.0") -> ValuationResult:
    _positive(inputs, "shares_outstanding")
    segments = inputs.get("segments", [])
    if not segments:
        raise ValuationError("SOTP必须提供segments")
    total = sum(float(item["value"]) for item in segments) - float(inputs.get("net_debt", 0))
    fair = total / float(inputs["shares_outstanding"])
    return _success(method_ref, fair, inputs)


def nav(inputs: dict[str, Any], method_ref: str = "VAL.NAV@1.0.0") -> ValuationResult:
    _positive(inputs, "shares_outstanding")
    assets = inputs.get("assets", [])
    if not assets:
        raise ValuationError("NAV必须提供assets")
    adjusted_assets = sum(float(item["value"]) * float(item.get("adjustment", 1)) for item in assets)
    net_assets = adjusted_assets - float(inputs.get("liabilities", 0))
    fair = net_assets / float(inputs["shares_outstanding"])
    return _success(method_ref, fair, inputs)


def reverse_dcf(inputs: dict[str, Any], method_ref: str = "VAL.REVERSE_DCF@1.0.0") -> ValuationResult:
    _positive(inputs, "base_fcff", "wacc", "shares_outstanding", "current_price")
    target_ev = float(inputs["current_price"]) * float(inputs["shares_outstanding"]) + float(inputs.get("net_debt", 0))
    low, high = -0.5, 1.0
    terminal_growth = float(inputs.get("terminal_growth", 0.02))
    years = int(inputs.get("years", 5))
    if years < 1 or years > 30:
        raise ValuationError("years必须在1至30之间")
    discount_rate = float(inputs["wacc"])
    base_fcff = float(inputs["base_fcff"])
    low_value = _dcf_value(_project_cashflows(base_fcff, [low] * years), discount_rate, terminal_growth)
    high_value = _dcf_value(_project_cashflows(base_fcff, [high] * years), discount_rate, terminal_growth)
    if not low_value <= target_ev <= high_value:
        raise ValuationError("给定增长率搜索区间内不存在有效根")
    for _ in range(100):
        mid = (low + high) / 2
        value = _dcf_value(_project_cashflows(base_fcff, [mid] * years), discount_rate, terminal_growth)
        if value < target_ev:
            low = mid
        else:
            high = mid
    implied = (low + high) / 2
    return _success(
        method_ref,
        float(inputs["current_price"]),
        {**inputs, "implied_fcff_growth": implied},
        sensitivity={"implied_fcff_growth": round(implied, 6)},
    )


def cycle_normalized(inputs: dict[str, Any], method_ref: str = "VAL.CYCLE_NORMALIZED@1.0.0") -> ValuationResult:
    _positive(inputs, "normalized_eps", "target_pe")
    fair = float(inputs["normalized_eps"]) * float(inputs["target_pe"])
    low = float(inputs.get("low_eps", inputs["normalized_eps"])) * float(inputs.get("low_pe", inputs["target_pe"]))
    high = float(inputs.get("high_eps", inputs["normalized_eps"])) * float(inputs.get("high_pe", inputs["target_pe"]))
    return _success(method_ref, fair, inputs, low, high)


def embedded_value(inputs: dict[str, Any], method_ref: str = "VAL.PEV@1.0.0") -> ValuationResult:
    _positive(inputs, "embedded_value_per_share", "target_pev")
    fair = float(inputs["embedded_value_per_share"]) * float(inputs["target_pev"])
    return _success(method_ref, fair, inputs)


MODEL_FUNCTIONS: dict[tuple[str, str], Callable[[dict[str, Any], str], ValuationResult]] = {
    ("VAL.RELATIVE", "1.0.0"): relative_valuation,
    ("VAL.DCF.FCFF", "1.0.0"): fcff_dcf,
    ("VAL.DCF.FCFE", "1.0.0"): fcfe_dcf,
    ("VAL.DDM", "1.0.0"): ddm,
    ("VAL.RESIDUAL_INCOME", "1.0.0"): residual_income,
    ("VAL.SOTP", "1.0.0"): sotp,
    ("VAL.NAV", "1.0.0"): nav,
    ("VAL.REVERSE_DCF", "1.0.0"): reverse_dcf,
    ("VAL.CYCLE_NORMALIZED", "1.0.0"): cycle_normalized,
    ("VAL.PEV", "1.0.0"): embedded_value,
}


def execute_valuation(method_id: str, version: str, inputs: dict[str, Any]) -> ValuationResult:
    method_ref = f"{method_id}@{version}"
    function = MODEL_FUNCTIONS.get((method_id, version))
    if function is None:
        return ValuationResult(
            method_ref=method_ref,
            status=ModelRunStatus.NOT_APPLICABLE,
            inputs=inputs,
            failure_reason=f"没有可执行估值实现: {method_id}@{version}",
        )
    try:
        return function(inputs, method_ref)
    except (ValuationError, ValueError, TypeError, ZeroDivisionError, KeyError, IndexError) as exc:
        return ValuationResult(
            method_ref=method_ref,
            status=ModelRunStatus.FAILED,
            inputs=inputs,
            failure_reason=str(exc),
        )
