from __future__ import annotations

from typing import Any

from .acquisition.registry import SourceRegistryLoader


def _legacy_provider_catalog() -> tuple[frozenset[str], tuple[str, ...]]:
    """Derive compatibility aliases from the versioned registry.

    This module remains as a legacy watchdog facade.  Formal business-model
    smoke runs are executed by ``AcquisitionOrchestrator.smoke_sources`` and
    persisted with a run lease; neither path owns a provider constant.
    """

    loader = SourceRegistryLoader()
    questions = loader.load_questions()
    registry = loader.load_registry(
        question_set=questions,
        expect_business_model_v1=4,
    ).registry
    pairs = tuple(
        (alias, definition.license_policy.access_cost.value)
        for definition in registry.legacy_definitions
        for alias in (definition.aliases or (definition.adapter_key,))
    )
    aliases = tuple(alias for alias, _ in pairs)
    providers = frozenset(aliases)
    defaults = tuple(alias for alias, access_cost in pairs if access_cost == "free")
    return providers, defaults


PROVIDERS, DEFAULT_PROVIDERS = _legacy_provider_catalog()


def probe_registered_sources(
    runtime: Any,
    ticker: str,
    source_ids: list[str] | None = None,
    *,
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    """Execute one durable registry-driven smoke run in an injected runtime.

    Typed outcomes, the run lease and the cross-process source gate belong to
    the acquisition orchestrator.  In particular this function never creates
    a runtime per source and never infers state from translated error text.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds必须大于0")
    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        raise RuntimeError("采集执行器尚不可用")
    smoke = getattr(orchestrator, "smoke_sources", None) or getattr(
        orchestrator, "execute_smoke", None
    )
    if smoke is None:
        raise RuntimeError("采集执行器未实现smoke_sources")
    result = smoke(ticker=ticker, source_ids=source_ids)
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    raise TypeError("smoke执行器返回了不支持的结果类型")


def probe_online_sources(
    ticker: str,
    providers: list[str],
    *,
    runtime: Any | None = None,
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    """Deprecated compatibility facade for registry-driven smoke runs.

    Historical callers passed only ``ticker`` and legacy provider names.  That
    implementation spawned one process per provider and constructed an
    ``AdapterManager`` inside each worker, bypassing the durable run, attempt,
    lease and cross-process source gate contracts.  Keep the callable surface
    so old imports fail with a useful migration message, but never construct a
    transport or runtime here.  Callers that inject the process-wide runtime
    are delegated to the single registry-driven path.
    """

    if runtime is None:
        unknown = sorted(set(providers) - PROVIDERS)
        if unknown:
            raise ValueError(f"未知适配器: {', '.join(unknown)}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds必须大于0")
    if runtime is None:
        raise RuntimeError(
            "probe_online_sources已禁用无运行时的直接联网；"
            "请注入AcquisitionRuntime或使用smoke-sources CLI"
        )
    return probe_registered_sources(
        runtime,
        ticker,
        list(providers),
        timeout_seconds=timeout_seconds,
    )
