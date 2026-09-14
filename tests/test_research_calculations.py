from copy import deepcopy
from decimal import Decimal

import pytest

from analysis.research.calculations import Calculations
from analysis.research.workspace import ResearchError


class FrozenInputs:
    def __init__(self):
        self.rows = []
        for ref, metric, value, unit, period, kind in [
            ("E", "parent_net_profit", 1000, "CNY", "2025-12-31", "cumulative"),
            ("S", "total_shares", 100, "shares", "2026-06-30", "instant"),
            ("P", "market_price", 150, "CNY_per_share", "2026-09-11", "instant"),
            ("R1", "operating_income", 100, "CNY", "2021-12-31", "cumulative"),
            ("R2", "operating_income", 146.41, "CNY", "2025-12-31", "cumulative"),
        ]:
            self.rows.append(dict(fact_ref=ref, metric_id=metric, period=period, period_type=kind,
                fact=dict(fact_id="fact-"+ref, value=str(value), unit=unit, currency="CNY", scope="consolidated", period_end=period)))

    def pack(self, rid):
        return {"snapshot_id": "frozen"}, None, {"metrics": self.rows}

    def artifact(self, rid, kind, payload):
        return {"artifact_id": "calc-test", "snapshot_id": "frozen", **payload}


def assumptions():
    return {
        "scenarios": {"reason": "测试情景，不是公司判断", "value": [
            dict(name=name, growth=g, multiple=m, reason="测试依据", valid_until="2026-12-31", invalidation=["测试变化"])
            for name,g,m in [("bear", -.1, 10), ("base", .1, 15), ("bull", .2, 20)]]},
        "sensitivity_growth": {"reason": "测试", "value": [-.1, .1]},
        "sensitivity_multiples": {"reason": "测试", "value": [10, 20]},
    }


def test_pe_independent_recalculation_and_recorded_price_date():
    calc = Calculations(FrozenInputs()).calculate("r", "pe_scenarios", {"earnings":"E", "shares":"S", "price":"P"}, assumptions())
    values = {s["name"]: Decimal(s["fair_value"]) for s in calc["result"]["scenarios"]}
    assert values == {"bear": Decimal(90), "base": Decimal(165), "bull": Decimal(240)}
    assert calc["result"]["price_fact_period"] == "2026-09-11"
    assert len(calc["result"]["sensitivity"]) == 4
    assert calc["input_fact_ids"]["earnings"] == "fact-E"


def test_cagr_uses_four_intervals_and_rejects_currency_mismatch():
    w = FrozenInputs()
    calc = Calculations(w)
    result = calc.calculate("r", "cagr", {"start":"R1", "end":"R2"})["result"]
    assert result["years"] == 4
    assert abs(Decimal(result["value"]) - Decimal(".1")) < Decimal("1e-20")
    w.rows[-1]["fact"]["currency"] = "USD"
    with pytest.raises(ResearchError, match="scope_mismatch"):
        calc.calculate("r", "cagr", {"start":"R1", "end":"R2"})


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "0"])
def test_pe_invalid_shares_fail_closed(bad):
    w = FrozenInputs()
    w.rows[1]["fact"]["value"] = bad
    with pytest.raises(ResearchError):
        Calculations(w).calculate("r", "pe_scenarios", {"earnings":"E", "shares":"S", "price":"P"}, assumptions())


def test_pe_does_not_silently_annualize_interim_profit():
    w = FrozenInputs()
    w.rows[0]["period"] = "2026-06-30"
    with pytest.raises(ResearchError, match="partial_year"):
        Calculations(w).calculate("r", "pe_scenarios", {"earnings":"E", "shares":"S", "price":"P"}, assumptions())
