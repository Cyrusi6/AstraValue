from __future__ import annotations

import pytest
import json
from pathlib import Path

from analysis.adapters.manager import AdapterManager, StructuredServiceUnavailable
from analysis.models import SyncRequest, SyncResult
from analysis.policies import load_source_policy


class StubStructuredService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def sync(self, ticker: str, options: SyncRequest) -> SyncResult:
        self.calls.append((ticker, tuple(options.datasets)))
        return SyncResult(
            ticker=ticker,
            scopes=list(options.scopes),
            provider_results={"structured": "planned"},
            source_strategy="structured-first-v1",
            structured_plan_id="structured-plan:1",
            default_consume_eligible=False,
        )


def test_omitted_providers_choose_new_default_but_explicit_providers_are_legacy() -> None:
    omitted = SyncRequest()
    explicit = SyncRequest(providers=["akshare", "baostock"])

    assert omitted.providers == ["official", "akshare", "sina", "baostock"]
    assert omitted.effective_source_strategy == "structured-first-v1"
    assert explicit.effective_source_strategy == "legacy-v1"


def test_new_default_fails_closed_without_bound_structured_service() -> None:
    with pytest.raises(StructuredServiceUnavailable, match="显式绑定"):
        AdapterManager().sync("600519", SyncRequest(scopes=["financials"]))


def test_structured_branch_does_not_fall_through_to_legacy_consolidation() -> None:
    service = StubStructuredService()
    result = AdapterManager(structured_service=service).sync(
        "600519",
        SyncRequest(scopes=["financials", "market"], datasets=["F01"]),
    )

    assert service.calls == [("600519", ("F01",))]
    assert result.source_strategy == "structured-first-v1"
    assert result.structured_plan_id == "structured-plan:1"
    assert result.raw_facts == []


def test_source_policy_v2_preserves_frozen_legacy_statuses() -> None:
    policy = load_source_policy()
    assert policy.default_strategy == "structured-first-v1"
    assert policy.legacy_strategy == "legacy-v1"
    assert "供应商直采" in policy.statuses

    path = Path("config/methods/source_policy.v1.json")
    legacy = json.loads(path.read_text(encoding="utf-8"))
    assert legacy["statuses"] == [
        "双源一致",
        "权威单源",
        "待核验",
        "估算",
        "未披露",
        "暂无该数据",
        "不适用",
    ]
