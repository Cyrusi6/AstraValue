import json

from analysis.registry import MethodRegistry, PROJECT_ROOT


def test_method_library_is_consistent():
    registry = MethodRegistry()
    assert registry.validate_library() == []


def test_method_bundle_is_deterministic_and_hashed():
    registry = MethodRegistry()
    first = registry.create_bundle(["STEP.BUSINESS", "VAL.RELATIVE"])
    second = registry.create_bundle(["VAL.RELATIVE", "STEP.BUSINESS", "VAL.RELATIVE"])
    assert first.method_bundle_id == second.method_bundle_id
    assert first.method_hashes == second.method_hashes


def test_metric_dictionary_has_required_contract_fields():
    data = json.loads((PROJECT_ROOT / "config" / "methods" / "metrics.json").read_text(encoding="utf-8"))
    assert {"revenue", "net_income_excl", "operating_cash_flow", "pe_ttm", "pb", "cr3", "cr5"} <= data["metrics"].keys()
    assert all({"name", "statement", "unit", "type"} <= item.keys() for item in data["metrics"].values())

