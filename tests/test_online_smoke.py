import pytest
from types import SimpleNamespace

import analysis.online_smoke as online_smoke
from analysis.adapters.manager import AdapterManager
from analysis.acquisition.registry import SourceRegistryLoader
from analysis.models import SyncRequest, SyncResult
from analysis.online_smoke import (
    DEFAULT_PROVIDERS,
    PROVIDERS,
    probe_online_sources,
    probe_registered_sources,
)


def test_online_smoke_rejects_unknown_provider_without_network():
    with pytest.raises(ValueError, match="未知适配器"):
        probe_online_sources("600519", ["unknown"])


def test_online_smoke_rejects_non_positive_timeout_without_network():
    with pytest.raises(ValueError, match="timeout_seconds"):
        probe_online_sources("600519", ["akshare"], timeout_seconds=0)


def test_legacy_online_smoke_requires_injected_runtime_and_does_zero_io(monkeypatch):
    delegated = []

    def unexpected_delegate(*args, **kwargs):
        delegated.append((args, kwargs))
        raise AssertionError("无runtime时不得进入任何smoke执行路径")

    monkeypatch.setattr(online_smoke, "probe_registered_sources", unexpected_delegate)

    with pytest.raises(RuntimeError, match="直接联网"):
        probe_online_sources("600519", ["akshare"])

    assert delegated == []


def test_legacy_online_smoke_with_runtime_delegates_once(monkeypatch):
    runtime = object()
    calls = []

    def registered(runtime_arg, ticker, source_ids, *, timeout_seconds):
        calls.append((runtime_arg, ticker, source_ids, timeout_seconds))
        return {"run_id": "run-smoke", "checkpoint_advanced": False}

    monkeypatch.setattr(online_smoke, "probe_registered_sources", registered)

    result = probe_online_sources(
        "300750",
        ["szse.disclosures"],
        runtime=runtime,
        timeout_seconds=7,
    )

    assert calls == [(runtime, "300750", ["szse.disclosures"], 7)]
    assert result == {"run_id": "run-smoke", "checkpoint_advanced": False}


def test_default_sources_include_sina_but_keep_tushare_opt_in():
    assert "sina" in DEFAULT_PROVIDERS
    assert "tushare" in PROVIDERS
    assert "tushare" not in DEFAULT_PROVIDERS
    assert SyncRequest().providers == ["official", "akshare", "sina", "baostock"]
    assert set(SyncRequest().providers) <= set(AdapterManager().adapters)


def test_announcement_only_sync_does_not_emit_market_multiple_warning():
    class AnnouncementOnlyAdapter:
        def sync(self, ticker, options):
            return SyncResult(
                ticker=ticker,
                scopes=options.scopes,
                provider_results={"official": "公告索引完成"},
                as_of=options.as_of,
            )

    manager = AdapterManager()
    manager.adapters = {"official": AnnouncementOnlyAdapter()}
    result = manager.sync(
        "600519",
        SyncRequest(providers=["official"], scopes=["announcements"]),
    )

    assert result.warnings == []
    assert result.facts == []


def test_online_smoke_catalog_is_derived_from_legacy_registry_policy():
    loader = SourceRegistryLoader()
    questions = loader.load_questions()
    registry = loader.load_registry(question_set=questions).registry
    expected = {
        alias
        for definition in registry.legacy_definitions
        for alias in definition.aliases
    }
    expected_defaults = {
        alias
        for definition in registry.legacy_definitions
        if definition.license_policy.access_cost.value == "free"
        for alias in definition.aliases
    }
    assert PROVIDERS == expected
    assert set(DEFAULT_PROVIDERS) == expected_defaults


def test_online_smoke_one_composition_root_per_process_delegates_once():
    calls = []

    class FakeOrchestrator:
        def smoke_sources(self, *, ticker, source_ids):
            calls.append((ticker, source_ids))
            return {
                "run_id": "run-smoke",
                "result": "succeeded",
                "coverage_accounted": True,
                "material_gap_count": 0,
                "default_consume_eligible": False,
                "checkpoint_advanced": False,
            }

    runtime = SimpleNamespace(orchestrator=FakeOrchestrator())
    result = probe_registered_sources(
        runtime,
        "300750",
        ["szse.disclosures"],
    )

    assert calls == [("300750", ["szse.disclosures"])]
    assert result["run_id"] == "run-smoke"
    assert result["checkpoint_advanced"] is False
    assert result["default_consume_eligible"] is False
