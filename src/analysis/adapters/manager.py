from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from ..acquisition.models import (
    AcquisitionAttemptEventType,
    AcquisitionMode,
    CoveragePlanDisposition,
)
from ..acquisition.registry import (
    DEFAULT_QUESTIONS_PATH,
    DEFAULT_REGISTRY_PATH,
    LoadedSourceRegistry,
    SourceRegistryLoader,
)
from ..market_multiples import derive_verified_market_multiples
from ..models import SyncRequest, SyncResult
from ..verification import consolidate_facts
from .akshare_adapter import AkshareAdapter
from .baostock_adapter import BaostockAdapter
from .official_adapter import OfficialDisclosureAdapter
from .sina_adapter import SinaFinanceAdapter
from .tushare_adapter import TushareProAdapter


LegacyAdapterBuilder = Callable[[], Any]


DEFAULT_LEGACY_ADAPTER_BUILDERS: dict[str, LegacyAdapterBuilder] = {
    # This is an installed-capability map, not a source-selection list.  The
    # versioned registry decides which definitions and aliases are exposed.
    "official": OfficialDisclosureAdapter,
    "akshare": AkshareAdapter,
    "sina": SinaFinanceAdapter,
    "baostock": BaostockAdapter,
    "tushare": TushareProAdapter,
}


class AdapterManager:
    """Registry-backed compatibility facade for the existing sync pipeline.

    ``DEFAULT_LEGACY_ADAPTER_BUILDERS`` describes code that is installed in
    this binary.  Provider identities, aliases and supported scopes come only
    from the frozen source registry.  The acquisition runtime is injected so
    the business-model path can share its namespace, repository and source
    gate instead of constructing another composition root.
    """

    def __init__(
        self,
        *,
        loaded_registry: LoadedSourceRegistry | None = None,
        acquisition_runtime: Any | None = None,
        legacy_adapter_builders: dict[str, LegacyAdapterBuilder] | None = None,
    ) -> None:
        if loaded_registry is None:
            loader = SourceRegistryLoader(
                known_adapter_keys={
                    "cninfo",
                    "sse",
                    "szse",
                    "moutai_ir",
                    *DEFAULT_LEGACY_ADAPTER_BUILDERS,
                }
            )
            questions = loader.load_questions(DEFAULT_QUESTIONS_PATH)
            loaded_registry = loader.load_registry(
                DEFAULT_REGISTRY_PATH,
                question_set=questions,
                expect_business_model_v1=4,
            )
        self.loaded_registry = loaded_registry
        self.acquisition_runtime = acquisition_runtime
        builders = dict(DEFAULT_LEGACY_ADAPTER_BUILDERS)
        if legacy_adapter_builders:
            builders.update(legacy_adapter_builders)

        instances: dict[str, Any] = {}
        adapters: dict[str, Any] = {}
        definition_ids: dict[str, str] = {}
        for definition in loaded_registry.registry.legacy_definitions:
            builder = builders.get(definition.adapter_key)
            if builder is None:
                # Registry validation normally rejects this.  Keeping the
                # facade fail-closed also protects explicitly injected test
                # registries.
                continue
            adapter = instances.setdefault(definition.adapter_key, builder())
            aliases = definition.aliases or (definition.adapter_key,)
            for alias in aliases:
                if alias in adapters and adapters[alias] is not adapter:
                    raise ValueError(f"来源注册表legacy alias冲突: {alias}")
                adapters[alias] = adapter
                definition_ids[alias] = definition.source_definition_id
        self.adapters = adapters
        self.legacy_definition_ids = definition_ids

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
        requested_scopes = set(options.scopes)
        business_model_requested = "business_model" in requested_scopes
        legacy_scopes = requested_scopes - {"business_model"}

        if business_model_requested and legacy_scopes:
            raise ValueError(
                "business_model不能与legacy同步范围混合执行；请分别发起采集运行和旧财务同步"
            )

        if business_model_requested:
            acquisition = self._sync_business_model(ticker, options)
            combined.provider_results.update(acquisition.provider_results)
            combined.acquisition_run_id = acquisition.acquisition_run_id
            combined.acquisition_status = acquisition.acquisition_status
            combined.provider_results_authority = acquisition.provider_results_authority
            combined.coverage_accounted = acquisition.coverage_accounted
            combined.material_gap_count = acquisition.material_gap_count
            combined.default_consume_eligible = acquisition.default_consume_eligible
            combined.checkpoint_ids = list(acquisition.checkpoint_ids)
            combined.raw_resource_snapshot_ids = list(
                acquisition.raw_resource_snapshot_ids
            )
            combined.warnings.extend(acquisition.warnings)

        if legacy_scopes:
            legacy_options = options.model_copy(
                update={"scopes": sorted(legacy_scopes)}
            )
            self._sync_legacy_into(combined, ticker, legacy_options)

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

    def _sync_legacy_into(
        self,
        combined: SyncResult,
        ticker: str,
        options: SyncRequest,
    ) -> None:
        for name in options.providers:
            adapter = self.adapters.get(name)
            if adapter is None:
                if self._alias_applies_to_any_scope(name, set(options.scopes)):
                    # A business-model-only alias is deliberately irrelevant to
                    # a legacy sync; it must never instantiate an arbitrary
                    # adapter or silently expand the legacy source set.
                    continue
                raise ValueError(f"未知或不适用于请求范围的来源alias: {name}")
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

    def _sync_business_model(self, ticker: str, options: SyncRequest) -> SyncResult:
        runtime = self.acquisition_runtime
        if runtime is None:
            raise RuntimeError(
                "business_model采集必须使用显式绑定database/data_root的AcquisitionRuntime"
            )
        if runtime.orchestrator is None:
            raise RuntimeError("business_model采集执行器不可用")

        # Provider aliases are compatibility input only.  They can neither
        # remove an applicable v1 source nor introduce legacy/paid sources.
        for alias in options.providers:
            if alias in self.adapters:
                continue
            if not self._alias_applies_to_any_scope(alias, {"business_model"}):
                raise ValueError(f"未知business_model来源alias: {alias}")

        mode = (
            AcquisitionMode(options.acquisition_mode)
            if options.acquisition_mode is not None
            else runtime.default_business_model_mode(ticker)
        )
        plan = runtime.plan_company_run(
            ticker,
            mode=mode,
            as_of=options.as_of,
            parent_run_id=options.acquisition_parent_run_id,
            persist=True,
        )
        if options.acquisition_plan_only:
            return SyncResult(
                ticker=ticker,
                scopes=list(options.scopes),
                as_of=options.as_of,
                provider_results=self._planned_source_summaries(plan),
                acquisition_run_id=plan.run.run_id,
                acquisition_status="planned",
                provider_results_authority="structured_attempts",
                coverage_accounted=False,
                material_gap_count=None,
                default_consume_eligible=False,
            )

        execute = getattr(runtime.orchestrator, "execute_run", None) or getattr(
            runtime.orchestrator, "execute", None
        )
        if execute is None:
            raise RuntimeError("business_model采集执行器接口不可用")
        execution = execute(plan.run.run_id)
        payload = (
            execution.as_dict()
            if hasattr(execution, "as_dict")
            else execution.model_dump(mode="json")
            if hasattr(execution, "model_dump")
            else dict(execution)
        )
        attempts = runtime.repository.list_attempts(run_id=plan.run.run_id)
        snapshot_ids = sorted(
            {
                observation.snapshot_id
                for observation in runtime.repository.list_resource_observations(
                    limit=100_000
                )
                if observation.attempt_id in {item.attempt_id for item in attempts}
                and observation.snapshot_id is not None
            }
        )
        return SyncResult(
            ticker=ticker,
            scopes=list(options.scopes),
            as_of=options.as_of,
            provider_results=self._executed_source_summaries(plan.run.run_id),
            acquisition_run_id=plan.run.run_id,
            acquisition_status=str(payload.get("result", "failed")),
            provider_results_authority="structured_attempts",
            coverage_accounted=bool(payload.get("coverage_accounted", False)),
            material_gap_count=int(payload.get("material_gap_count", 0)),
            default_consume_eligible=bool(
                payload.get("default_consume_eligible", False)
            ),
            checkpoint_ids=list(payload.get("checkpoint_ids") or ()),
            raw_resource_snapshot_ids=snapshot_ids,
        )

    def _planned_source_summaries(self, plan: Any) -> dict[str, str]:
        by_source: dict[str, Counter[str]] = defaultdict(Counter)
        for entry in plan.coverage_entries:
            key = (
                entry.static_reason_code
                if entry.plan_disposition
                == CoveragePlanDisposition.STATIC_POLICY_SKIPPED
                else "required"
            )
            by_source[entry.source_definition_id][str(key)] += 1
        return {
            source_id: _canonical_summary("planned", counts)
            for source_id, counts in sorted(by_source.items())
        }

    def _executed_source_summaries(self, run_id: str) -> dict[str, str]:
        repository = self.acquisition_runtime.repository
        by_source: dict[str, Counter[str]] = defaultdict(Counter)
        for attempt in repository.list_attempts(run_id=run_id):
            events = repository.list_attempt_events(attempt.attempt_id)
            terminal = next(
                (
                    event
                    for event in reversed(events)
                    if event.event_type
                    in {
                        AcquisitionAttemptEventType.OUTCOME_TERMINAL,
                        AcquisitionAttemptEventType.ABANDONED,
                    }
                ),
                None,
            )
            if terminal is None:
                by_source[attempt.source_definition_id]["started"] += 1
            elif terminal.event_type == AcquisitionAttemptEventType.ABANDONED:
                by_source[attempt.source_definition_id]["abandoned"] += 1
            else:
                by_source[attempt.source_definition_id][terminal.outcome.value] += 1
        run = repository.get_run(run_id)
        for entry in repository.list_coverage_entries(run_id):
            if entry.plan_disposition == CoveragePlanDisposition.STATIC_POLICY_SKIPPED:
                by_source[entry.source_definition_id][entry.static_reason_code] += 1
        # Preserve the complete frozen v1 source set even when a source was
        # statically inapplicable/disabled and therefore correctly had zero I/O.
        for ref in run.source_definition_refs:
            by_source.setdefault(ref.source_definition_id, Counter())
        return {
            source_id: _canonical_summary("attempts", counts)
            for source_id, counts in sorted(by_source.items())
        }

    def _alias_applies_to_any_scope(self, alias: str, scopes: set[str]) -> bool:
        return any(
            entry.alias == alias and bool(scopes.intersection(entry.scopes))
            for entry in self.loaded_registry.registry.aliases
        )


def _deduplicate_and_pin(records, key, data_snapshot_id: str):
    key_fn = key if callable(key) else lambda item: getattr(item, key)
    deduplicated = {key_fn(item): item for item in records}
    return [
        item
        if item.data_snapshot_id == data_snapshot_id
        else item.model_copy(update={"data_snapshot_id": data_snapshot_id})
        for item in deduplicated.values()
    ]


def _canonical_summary(kind: str, counts: Counter[str]) -> str:
    return json.dumps(
        {"kind": kind, "counts": dict(sorted(counts.items()))},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
