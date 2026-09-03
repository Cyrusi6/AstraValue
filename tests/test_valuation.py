import pytest

from analysis.models import ModelRunStatus
from analysis.valuation import (
    ValuationError,
    cycle_normalized,
    ddm,
    embedded_value,
    execute_valuation,
    fcfe_dcf,
    fcff_dcf,
    nav,
    relative_valuation,
    residual_income,
    reverse_dcf,
    sotp,
)


NORMAL_MODEL_CASES = {
    "VAL.RELATIVE": {"metric_per_share": 2, "target_multiple": 10},
    "VAL.DCF.FCFF": {"base_fcff": 100, "wacc": 0.1, "shares_outstanding": 10},
    "VAL.DCF.FCFE": {"base_fcfe": 80, "cost_of_equity": 0.11, "shares_outstanding": 10},
    "VAL.DDM": {"dividend_per_share": 1.5, "cost_of_equity": 0.1},
    "VAL.RESIDUAL_INCOME": {"book_value_per_share": 12, "roe": 0.14, "cost_of_equity": 0.1},
    "VAL.SOTP": {"segments": [{"value": 100}], "shares_outstanding": 10},
    "VAL.NAV": {"assets": [{"value": 100}], "liabilities": 20, "shares_outstanding": 10},
    "VAL.REVERSE_DCF": {"base_fcff": 100, "wacc": 0.1, "shares_outstanding": 10, "current_price": 163.6609, "years": 5, "terminal_growth": 0.02},
    "VAL.CYCLE_NORMALIZED": {"normalized_eps": 2, "target_pe": 9},
    "VAL.PEV": {"embedded_value_per_share": 20, "target_pev": 0.9},
}

EXTREME_VALID_CASES = {
    "VAL.RELATIVE": {"metric_per_share": 1e-9, "target_multiple": 1e9},
    "VAL.DCF.FCFF": {"base_fcff": 1, "wacc": 0.0201, "shares_outstanding": 1, "terminal_growth": 0.02},
    "VAL.DCF.FCFE": {"base_fcfe": 1, "cost_of_equity": 0.0201, "shares_outstanding": 1, "terminal_growth": 0.02},
    "VAL.DDM": {"dividend_per_share": 1, "cost_of_equity": 0.0201, "terminal_growth": 0.02},
    "VAL.RESIDUAL_INCOME": {"book_value_per_share": 1, "roe": 1, "cost_of_equity": 0.0201, "terminal_growth": 0.02, "years": 30},
    "VAL.SOTP": {"segments": [{"value": 1e18}], "shares_outstanding": 1e9},
    "VAL.NAV": {"assets": [{"value": 1e18, "adjustment": 0.01}], "shares_outstanding": 1e9},
    "VAL.REVERSE_DCF": {"base_fcff": 1, "wacc": 0.5, "shares_outstanding": 1, "current_price": 1, "years": 1, "terminal_growth": 0},
    "VAL.CYCLE_NORMALIZED": {"normalized_eps": 1e-6, "target_pe": 1e6},
    "VAL.PEV": {"embedded_value_per_share": 1e-6, "target_pev": 1e6},
}

NEGATIVE_OR_INVALID_CASES = {
    "VAL.RELATIVE": {"metric_per_share": -1, "target_multiple": 10},
    "VAL.DCF.FCFF": {"base_fcff": -1, "wacc": 0.1, "shares_outstanding": 10},
    "VAL.DCF.FCFE": {"base_fcfe": -1, "cost_of_equity": 0.1, "shares_outstanding": 10},
    "VAL.DDM": {"dividend_per_share": -1, "cost_of_equity": 0.1},
    "VAL.RESIDUAL_INCOME": {"book_value_per_share": -1, "roe": 0.1, "cost_of_equity": 0.1},
    "VAL.SOTP": {"segments": [{"value": 100}], "shares_outstanding": -1},
    "VAL.NAV": {"assets": [{"value": 100}], "shares_outstanding": -1},
    "VAL.REVERSE_DCF": {"base_fcff": -1, "wacc": 0.1, "shares_outstanding": 10, "current_price": 10},
    "VAL.CYCLE_NORMALIZED": {"normalized_eps": -1, "target_pe": 10},
    "VAL.PEV": {"embedded_value_per_share": -1, "target_pev": 1},
}


def test_relative_valuation():
    result = relative_valuation({"metric_per_share": 2, "target_multiple": 10, "low_multiple": 8, "high_multiple": 12})
    assert result.fair_value_per_share == 20
    assert (result.range_low, result.range_high) == (16, 24)
    with pytest.raises(ValuationError):
        relative_valuation({"metric_per_share": -1, "target_multiple": 10})


def test_fcff_dcf():
    result = fcff_dcf({"base_fcff": 100, "wacc": 0.1, "shares_outstanding": 10, "growth_rates": [0.05] * 5, "terminal_growth": 0.02, "net_debt": 50})
    assert result.status == ModelRunStatus.SUCCESS
    assert result.fair_value_per_share > 0
    assert len(result.sensitivity) == 9
    with pytest.raises(ValuationError):
        fcff_dcf({"base_fcff": 100, "wacc": 0.02, "shares_outstanding": 10, "terminal_growth": 0.02})


def test_fcfe_dcf():
    result = fcfe_dcf({"base_fcfe": 80, "cost_of_equity": 0.11, "shares_outstanding": 10, "growth_rates": [0.04] * 5})
    assert result.status == ModelRunStatus.SUCCESS


def test_ddm():
    result = ddm({"dividend_per_share": 1.5, "cost_of_equity": 0.1, "growth_rates": [0.04] * 5, "terminal_growth": 0.02})
    assert result.fair_value_per_share > 0


def test_residual_income():
    result = residual_income({"book_value_per_share": 12, "roe": 0.14, "cost_of_equity": 0.1, "payout_ratio": 0.35})
    assert result.fair_value_per_share > 12


def test_sotp():
    result = sotp({"segments": [{"value": 100}, {"value": 50}], "net_debt": 20, "shares_outstanding": 10})
    assert result.fair_value_per_share == 13


def test_nav():
    result = nav({"assets": [{"value": 100, "adjustment": 0.8}, {"value": 50}], "liabilities": 30, "shares_outstanding": 10})
    assert result.fair_value_per_share == 10


def test_reverse_dcf():
    forward = fcff_dcf({"base_fcff": 100, "wacc": 0.1, "shares_outstanding": 10, "growth_rates": [0.08] * 5, "terminal_growth": 0.02})
    result = reverse_dcf({"base_fcff": 100, "wacc": 0.1, "shares_outstanding": 10, "current_price": forward.fair_value_per_share, "years": 5, "terminal_growth": 0.02})
    assert result.sensitivity["implied_fcff_growth"] == pytest.approx(0.08, abs=1e-5)


def test_cycle_normalized():
    result = cycle_normalized({"normalized_eps": 2, "target_pe": 9, "low_eps": 1.5, "high_eps": 2.5, "low_pe": 7, "high_pe": 11})
    assert result.fair_value_per_share == 18
    assert result.range_low == 10.5
    assert result.range_high == 27.5


def test_embedded_value():
    assert embedded_value({"embedded_value_per_share": 20, "target_pev": 0.9}).fair_value_per_share == 18


def test_failure_missing_negative_and_unknown_version():
    missing = execute_valuation("VAL.RELATIVE", "1.0.0", {"target_multiple": 10})
    negative = execute_valuation("VAL.DCF.FCFF", "1.0.0", {"base_fcff": -1, "wacc": 0.1, "shares_outstanding": 10})
    unknown = execute_valuation("VAL.RELATIVE", "9.9.9", {"metric_per_share": 1, "target_multiple": 10})
    assert missing.status == ModelRunStatus.FAILED
    assert negative.status == ModelRunStatus.FAILED
    assert unknown.status == ModelRunStatus.NOT_APPLICABLE


@pytest.mark.parametrize(("method_id", "inputs"), NORMAL_MODEL_CASES.items())
def test_every_valuation_model_has_a_normal_success_case(method_id, inputs):
    assert execute_valuation(method_id, "1.0.0", inputs).status == ModelRunStatus.SUCCESS


@pytest.mark.parametrize(("method_id", "inputs"), EXTREME_VALID_CASES.items())
def test_every_valuation_model_handles_an_extreme_but_valid_case(method_id, inputs):
    result = execute_valuation(method_id, "1.0.0", inputs)
    assert result.status == ModelRunStatus.SUCCESS, result.failure_reason


@pytest.mark.parametrize(("method_id", "inputs"), NEGATIVE_OR_INVALID_CASES.items())
def test_every_valuation_model_stops_on_negative_or_invalid_core_input(method_id, inputs):
    assert execute_valuation(method_id, "1.0.0", inputs).status == ModelRunStatus.FAILED


@pytest.mark.parametrize("method_id", NORMAL_MODEL_CASES)
def test_every_valuation_model_stops_on_missing_inputs(method_id):
    assert execute_valuation(method_id, "1.0.0", {}).status == ModelRunStatus.FAILED


@pytest.mark.parametrize("method_id", NORMAL_MODEL_CASES)
def test_every_valuation_model_marks_unknown_version_not_applicable(method_id):
    assert execute_valuation(method_id, "9.9.9", NORMAL_MODEL_CASES[method_id]).status == ModelRunStatus.NOT_APPLICABLE
