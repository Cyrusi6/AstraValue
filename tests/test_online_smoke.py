import pytest

from analysis.adapters.manager import AdapterManager
from analysis.models import SyncRequest, SyncResult
from analysis.online_smoke import DEFAULT_PROVIDERS, PROVIDERS, probe_online_sources


def test_online_smoke_rejects_unknown_provider_without_network():
    with pytest.raises(ValueError, match="未知适配器"):
        probe_online_sources("600519", ["unknown"])


def test_online_smoke_rejects_non_positive_timeout_without_network():
    with pytest.raises(ValueError, match="timeout_seconds"):
        probe_online_sources("600519", ["akshare"], timeout_seconds=0)


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
