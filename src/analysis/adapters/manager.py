from __future__ import annotations

from ..market_multiples import derive_verified_market_multiples
from ..models import SyncRequest, SyncResult
from ..verification import consolidate_facts
from .akshare_adapter import AkshareAdapter
from .baostock_adapter import BaostockAdapter
from .official_adapter import OfficialDisclosureAdapter
from .sina_adapter import SinaFinanceAdapter
from .tushare_adapter import TushareProAdapter


class AdapterManager:
    def __init__(self) -> None:
        self.adapters = {
            "official": OfficialDisclosureAdapter(),
            "akshare": AkshareAdapter(),
            "sina": SinaFinanceAdapter(),
            "baostock": BaostockAdapter(),
            "tushare": TushareProAdapter(),
        }

    def sync(
        self,
        ticker: str,
        options: SyncRequest | list[str] | None = None,
    ) -> SyncResult:
        # Keep list[str] compatibility for callers from the first prototype.
        if isinstance(options, list):
            options = SyncRequest(providers=options)
        options = options or SyncRequest()
        combined = SyncResult(
            ticker=ticker,
            provider_results={},
            scopes=options.scopes,
            as_of=options.as_of,
        )
        for name in options.providers:
            adapter = self.adapters.get(name)
            if adapter is None:
                combined.provider_results[name] = "未知适配器"
                combined.warnings.append(f"未知适配器: {name}")
                continue
            try:
                result = adapter.sync(ticker, options)
                combined.provider_results.update(result.provider_results)
                if result.company_name and not combined.company_name:
                    combined.company_name = result.company_name
                combined.sources.extend(result.sources)
                combined.facts.extend(result.facts)
                combined.documents.extend(result.documents)
                combined.dimensional_facts.extend(result.dimensional_facts)
                combined.events.extend(result.events)
                combined.industry_facts.extend(result.industry_facts)
                combined.forecast_snapshots.extend(result.forecast_snapshots)
                combined.peer_sets.extend(result.peer_sets)
                combined.announcements.extend(result.announcements)
                combined.warnings.extend(result.warnings)
            except Exception as exc:
                combined.provider_results[name] = f"失败: {exc}"
                combined.warnings.append(f"{name}: {exc}")
        combined.sources = list({item.source_id: item for item in combined.sources}.values())
        combined.documents = list({item.document_id: item for item in combined.documents}.values())
        combined.dimensional_facts = _deduplicate_and_pin(
            combined.dimensional_facts,
            "dimensional_fact_id",
            combined.sync_result_id,
        )
        combined.events = _deduplicate_and_pin(
            combined.events,
            "event_id",
            combined.sync_result_id,
        )
        combined.industry_facts = _deduplicate_and_pin(
            combined.industry_facts,
            "industry_fact_id",
            combined.sync_result_id,
        )
        combined.forecast_snapshots = _deduplicate_and_pin(
            combined.forecast_snapshots,
            "forecast_snapshot_id",
            combined.sync_result_id,
        )
        combined.peer_sets = _deduplicate_and_pin(
            combined.peer_sets,
            lambda item: f"{item.peer_set_id}@{item.version}",
            combined.sync_result_id,
        )
        combined.announcements = _deduplicate_and_pin(
            combined.announcements,
            "announcement_record_id",
            combined.sync_result_id,
        )
        provider_facts = list(combined.facts)
        verified_inputs, _ = consolidate_facts(provider_facts, combined.sources)
        derived_market_facts = []
        if "market" in options.scopes:
            multiples = derive_verified_market_multiples(
                verified_inputs,
                provider_facts,
                combined.sources,
            )
            combined.warnings.extend(multiples.warnings)
            derived_market_facts = multiples.facts
        combined.raw_facts = [*provider_facts, *derived_market_facts]
        combined.facts, combined.verification_records = consolidate_facts(
            combined.raw_facts,
            combined.sources,
        )
        return combined


def _deduplicate_and_pin(records, key, data_snapshot_id: str):
    key_fn = key if callable(key) else lambda item: getattr(item, key)
    deduplicated = {key_fn(item): item for item in records}
    return [
        item
        if item.data_snapshot_id == data_snapshot_id
        else item.model_copy(update={"data_snapshot_id": data_snapshot_id})
        for item in deduplicated.values()
    ]
