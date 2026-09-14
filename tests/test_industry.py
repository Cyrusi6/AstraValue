from analysis.industry import route_industry


def test_industry_routes_and_suffixes():
    assert route_industry("银行业").key == "bank"
    assert "VAL.RESIDUAL_INCOME" in route_industry("银行").valuation_method_ids
    assert route_industry("制造业").key == "manufacturing"
    unknown = route_industry("未知细分")
    assert unknown.key == "unknown"
    assert unknown.valuation_method_ids == ()


def test_every_route_has_registered_methods():
    from analysis.registry import MethodRegistry

    registry = MethodRegistry()
    for route in registry.industry_routes["routes"].values():
        registry.get(route["industry_method_id"])
        for method_id in route["valuation_method_ids"]:
            registry.get(method_id)
