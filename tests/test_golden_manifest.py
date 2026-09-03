import json

from analysis.golden import validate_golden_manifest
from analysis.registry import PROJECT_ROOT


def test_golden_manifest_covers_all_routes_without_claiming_validation():
    data = json.loads((PROJECT_ROOT / "data" / "golden" / "manifest.json").read_text(encoding="utf-8"))
    categories = {item["category"] for item in data["samples"]}
    assert {"普通制造", "消费", "科技", "银行", "保险", "券商", "地产", "资源周期", "公用事业", "尚未盈利"} <= categories
    assert data["acceptance"]["minimum_manually_checked_facts"] >= 50
    assert all(item["status"] == "pending_manual_validation" for item in data["samples"])


def test_pending_golden_manifest_passes_structure_but_not_acceptance():
    result = validate_golden_manifest(PROJECT_ROOT / "data" / "golden" / "manifest.json")
    assert result["passed"] is True
    assert result["structurally_valid"] is True
    assert result["ready_for_acceptance"] is False
    assert result["pending_count"] == 10


def test_strict_golden_validation_rejects_pending_samples():
    result = validate_golden_manifest(
        PROJECT_ROOT / "data" / "golden" / "manifest.json",
        strict=True,
    )
    assert result["passed"] is False
    assert result["ready_for_acceptance"] is False
