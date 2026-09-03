from copy import deepcopy

from analysis.models import ScenarioName
from analysis.scenarios import build_scenario_projections


def test_three_scenarios_are_deterministic(demo_request):
    first = build_scenario_projections(demo_request.assumptions)
    second = build_scenario_projections(deepcopy(demo_request.assumptions))
    assert first == second
    assert [item["scenario"] for item in first] == ["悲观", "基准", "乐观"]
    assert all(item["status"] == "已计算" for item in first)
    assert first[0]["fair_value_per_share"] < first[1]["fair_value_per_share"] < first[2]["fair_value_per_share"]
    assert "net_income_excl_parent" in first[1]["projections"][0]


def test_missing_and_stop_conditions(demo_request):
    missing = [item for item in demo_request.assumptions if not (item.scenario == ScenarioName.BEAR and item.name == "wacc")]
    results = build_scenario_projections(missing)
    assert results[0]["status"] == "暂无该数据"
    invalid = deepcopy(demo_request.assumptions)
    for item in invalid:
        if item.scenario == ScenarioName.BASE and item.name == "wacc":
            item.value = 0.01
        if item.scenario == ScenarioName.BASE and item.name == "terminal_growth":
            item.value = 0.02
    results = build_scenario_projections(invalid)
    assert results[1]["status"] == "待核验"

