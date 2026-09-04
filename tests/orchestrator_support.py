from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from analysis.acquisition.adapters.base import (
    BoundedTransportEnvelope,
    DiscoveryResult,
    FetchWork,
    NormalizedResource,
    QueryWork,
)
from analysis.acquisition.models import (
    AcquisitionMode,
    AcquisitionPlan,
    AcquisitionRun,
    AcquisitionRunKind,
)
from analysis.acquisition.orchestrator import AcquisitionOrchestrator
from analysis.acquisition.registry import INITIAL_REGISTRY_PATH
from analysis.acquisition.runtime import AcquisitionRuntime


NOW = datetime.now(timezone.utc)


class NullTransport:
    def close(self) -> None:
        return None


@dataclass
class ScenarioAdapter:
    discovery_response: Callable[[QueryWork], BoundedTransportEnvelope]
    discovery_result: Callable[[BoundedTransportEnvelope, QueryWork], DiscoveryResult]
    fetch_response: Callable[[FetchWork], BoundedTransportEnvelope] | None = None

    adapter_key: str = "fixture"

    def __post_init__(self) -> None:
        self.query_calls: list[QueryWork] = []
        self.fetch_calls: list[FetchWork] = []

    def execute_query(self, work: QueryWork) -> BoundedTransportEnvelope:
        self.query_calls.append(work)
        return self.discovery_response(work)

    def validate_and_normalize_without_retention(
        self,
        envelope: BoundedTransportEnvelope,
        work: QueryWork,
    ) -> DiscoveryResult:
        return self.discovery_result(envelope, work)

    def parse_retained_discovery(self, _snapshot_id: str, _work: QueryWork):
        raise AssertionError("fixture registry uses minimal-proof discovery")

    def fetch_resource(self, work: FetchWork) -> BoundedTransportEnvelope:
        self.fetch_calls.append(work)
        if self.fetch_response is None:
            raise AssertionError("unexpected fetch")
        return self.fetch_response(work)


def envelope(
    *,
    url: str,
    body: bytes = b"{}",
    status: int = 200,
    content_type: str = "application/json",
    headers: dict[str, str] | None = None,
    observed_at: datetime = NOW,
) -> BoundedTransportEnvelope:
    merged = {"content-type": content_type, **(headers or {})}
    return BoundedTransportEnvelope(
        request_url=url,
        final_url=url,
        status_code=status,
        headers=merged,
        body=body,
        observed_at=observed_at,
        retrieved_at=observed_at + timedelta(milliseconds=1),
        body_limit=max(1, len(body)),
    )


def resource(
    canonical_id: str = "fixture-resource-1",
    *,
    url: str = "https://static.cninfo.com.cn/finalpage/fixture.pdf",
    published_at: datetime = NOW - timedelta(days=1),
    required_fetch: bool = True,
) -> NormalizedResource:
    row_hash = hashlib.sha256(
        f"{canonical_id}|{url}|{published_at.isoformat()}".encode("utf-8")
    ).hexdigest()
    return NormalizedResource(
        canonical_resource_id=canonical_id,
        upstream_material_id=canonical_id,
        title=f"fixture {canonical_id}",
        url=url,
        published_raw=published_at.isoformat(),
        published_at=published_at,
        published_at_precision="instant",
        source_timezone="Asia/Shanghai",
        row_locator=f"row:{canonical_id}",
        row_hash=row_hash,
        required_fetch=required_fetch,
        metadata={"expected_mime_types": ("application/pdf",)},
    )


def discovery_result(
    work: QueryWork,
    *,
    resources: tuple[NormalizedResource, ...] = (),
    declared_total: int | None = 0,
    page_count: int | None = 1,
    next_cursor: str | None = None,
    terminal: bool = True,
) -> DiscoveryResult:
    body = f"{work.query_id}:{work.page}:{work.cursor}".encode("utf-8")
    return DiscoveryResult(
        resources=resources,
        schema_valid=True,
        parser_schema_version=work.parser_schema_version,
        declared_total=declared_total,
        normalized_total=len(resources),
        page=work.page,
        page_count=page_count,
        next_cursor=next_cursor,
        terminal=terminal,
        response_sha256=hashlib.sha256(body).hexdigest(),
        response_length=len(body),
        replayable=False,
    )


def no_data_adapter(
    *,
    before_query: Callable[[QueryWork], None] | None = None,
) -> ScenarioAdapter:
    def response(work: QueryWork) -> BoundedTransportEnvelope:
        if before_query is not None:
            before_query(work)
        return envelope(url=work.url)

    def parsed(_envelope: BoundedTransportEnvelope, work: QueryWork) -> DiscoveryResult:
        return discovery_result(work)

    return ScenarioAdapter(response, parsed)


def make_runtime(
    root: Path,
    adapter: ScenarioAdapter,
    *,
    registry_path: Path | None = None,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    lease_ttl_seconds: int = 60,
    heartbeat_interval_seconds: float = 60.0,
) -> AcquisitionRuntime:
    root.mkdir(parents=True, exist_ok=True)
    runtime_kwargs = {
        "workspace_root": root / "workspace",
        "orchestrator_factory": lambda _runtime: None,
    }
    runtime_kwargs["registry_path"] = registry_path or INITIAL_REGISTRY_PATH
    runtime = AcquisitionRuntime.create(
        root / "analysis.db",
        root / "data",
        **runtime_kwargs,
    )
    runtime.orchestrator = AcquisitionOrchestrator(
        runtime,
        transport_factory=lambda _definition: NullTransport(),
        adapter_resolver=lambda _definition, _transport: adapter,
        # Runtime/lease time must advance during longer combined test runs.
        # ``NOW`` remains the deterministic plan and response fixture anchor.
        now=now or (lambda: datetime.now(timezone.utc)),
        monotonic=monotonic,
        sleep=sleep,
        lease_ttl_seconds=lease_ttl_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )
    return runtime


def targeted_plan(
    runtime: AcquisitionRuntime,
    *,
    source_id: str = "cninfo.disclosures",
    query_id: str = "cninfo.periodic_report",
    ticker: str = "600519",
    mode: AcquisitionMode | str = AcquisitionMode.INCREMENTAL,
    run_kind: AcquisitionRunKind | str = AcquisitionRunKind.PRODUCTION,
    start_at: datetime = NOW - timedelta(days=1),
    as_of: datetime = NOW,
    parent_run_id: str | None = None,
    persist: bool = True,
) -> AcquisitionPlan:
    selected_mode = AcquisitionMode(mode)
    profile = runtime.build_profile(
        ticker,
        company_name="贵州茅台" if ticker == "600519" else ticker,
        listing_date=start_at.date(),
    )
    kwargs = {
        "mode": selected_mode,
        "as_of": as_of,
        "run_kind": AcquisitionRunKind(run_kind),
        "parent_run_id": parent_run_id,
        "storage_namespace_id": runtime.namespace_id,
    }
    if selected_mode != AcquisitionMode.BASELINE:
        kwargs["start_at"] = start_at
    if selected_mode == AcquisitionMode.RECONCILE:
        kwargs["reconcile_target"] = {
            "strategy": "fixture_exact_range",
            "parent_run_id": parent_run_id,
            "start_at": start_at.isoformat(),
        }
    base = runtime.planner.plan(profile, **kwargs)
    plans = tuple(
        item
        for item in base.physical_query_plan_items
        if item.source_definition_id == source_id and item.query_id == query_id
    )
    if len(plans) != 1:
        raise AssertionError(
            f"expected one targeted plan, got {len(plans)} for {source_id}/{query_id}"
        )
    plan_ids = {item.plan_item_id for item in plans}
    links = tuple(item for item in base.coverage_links if item.plan_item_id in plan_ids)
    coverage_ids = {item.coverage_entry_id for item in links}
    coverage = tuple(
        item for item in base.coverage_entries if item.coverage_entry_id in coverage_ids
    )
    refs = tuple(
        item
        for item in base.run.source_definition_refs
        if item.source_definition_id == source_id
    )
    run = AcquisitionRun.model_validate(
        {**base.run.model_dump(mode="python"), "source_definition_refs": refs}
    )
    plan = AcquisitionPlan(
        run=run,
        coverage_entries=coverage,
        physical_query_plan_items=plans,
        coverage_links=links,
    )
    if persist:
        runtime.orchestrator.persist_plan(plan)
    return plan
