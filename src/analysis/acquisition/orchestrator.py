from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from .adapters.base import (
    BoundedTransportEnvelope,
    DiscoveryResult as AdapterDiscoveryResult,
    FetchWork,
    NormalizedResource as AdapterNormalizedResource,
    QueryWork,
    TransportExecutionCapability,
)
from .checkpoints import (
    BarrierDraft,
    CheckpointEngine,
    PlanProgress,
    barrier_for_attempt,
    is_blocking_outcome,
    static_coverage_resolution,
)
from .discovery import (
    BoundedDiscoveryEnvelope,
    DiscoveryPageContext,
    DiscoveryPipeline,
    DiscoveryPipelineError,
    DiscoveryValidationError,
    NormalizedDiscoveryPage,
    NormalizedResource,
    _declared_page_count_matches,
)
from .models import (
    AcquisitionAttempt,
    AcquisitionAttemptEvent,
    AcquisitionAttemptEventType,
    AcquisitionAttemptSegment,
    AcquisitionMode,
    AcquisitionOutcome,
    AcquisitionPlan,
    AcquisitionRun,
    AcquisitionRunEvent,
    AcquisitionRunEventType,
    AcquisitionRunKind,
    AcquisitionRunResult,
    AttemptKind,
    BarrierResolution,
    CoverageEntry,
    CoveragePlanDisposition,
    CoverageResolution,
    CoverageResolutionStatus,
    DiscoveryBodyPolicy,
    DiscoveryProof,
    DiscoveredResource,
    PhysicalQueryCoverageLink,
    PhysicalQueryPlanItem,
    PublishedAtPrecision,
    ResourceDisposition,
    ResourceObservation,
    SourceDefinition,
    SourceQueryDefinition,
    ValidatorAnchor,
    acquisition_new_id,
    canonical_json_sha256,
    stable_acquisition_id,
)
from .repository import LeaseConflictError, StaleLeaseError
from .registry import SourceRegistryError, SourceRegistryLoader
from .security import SecurityPolicyError, sanitize_http_metadata
from .snapshots import (
    ArchiveWriteError,
    ContentSnapshotRequest,
    SnapshotCommitError,
    SnapshotIntegrityMismatch,
    SnapshotPipelineError,
)
from .status_classifier import (
    AttemptClassification,
    classify_discovery_completion,
    classify_exception,
    classify_fetch_result,
    classify_response,
)
from .transport import RegistryBoundHttpTransport


class AcquisitionExecutionError(RuntimeError):
    pass


class AcquisitionNamespaceMismatch(AcquisitionExecutionError):
    pass


class RunAlreadyFinalized(AcquisitionExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class AcquisitionExecutionResult:
    run_id: str
    result: AcquisitionRunResult
    coverage_accounted: bool
    material_gap_count: int
    default_consume_eligible: bool
    checkpoint_advanced: bool
    lease_epoch: int
    outcome_counts: Mapping[str, int]
    attempt_ids: tuple[str, ...]
    coverage_resolution_ids: tuple[str, ...]
    checkpoint_ids: tuple[str, ...]
    manifest_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "result": self.result.value,
            "coverage_accounted": self.coverage_accounted,
            "material_gap_count": self.material_gap_count,
            "default_consume_eligible": self.default_consume_eligible,
            "checkpoint_advanced": self.checkpoint_advanced,
            "lease_epoch": self.lease_epoch,
            "outcome_counts": dict(self.outcome_counts),
            "attempt_ids": list(self.attempt_ids),
            "coverage_resolution_ids": list(self.coverage_resolution_ids),
            "checkpoint_ids": list(self.checkpoint_ids),
            "manifest_id": self.manifest_id,
        }


@dataclass(slots=True)
class _AttemptFact:
    attempt: AcquisitionAttempt
    outcome: AcquisitionOutcome
    reason_code: str
    proof_ids: tuple[str, ...] = ()
    snapshot_ids: tuple[str, ...] = ()
    resource_observation_ids: tuple[str, ...] = ()
    work_position: str | None = None


@dataclass(slots=True)
class _PlanExecution:
    plan_item: PhysicalQueryPlanItem
    discovery_complete: bool = False
    discovery_outcome: AcquisitionOutcome | None = None
    discovery_attempt_ids: list[str] = field(default_factory=list)
    fetch_attempt_ids: list[str] = field(default_factory=list)
    proof_ids: list[str] = field(default_factory=list)
    discovery_snapshot_ids: list[str] = field(default_factory=list)
    content_snapshot_ids: list[str] = field(default_factory=list)
    resource_observation_ids: list[str] = field(default_factory=list)
    resources: dict[str, DiscoveredResource] = field(default_factory=dict)
    failed_resource_ids: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    barriers: list[BarrierDraft] = field(default_factory=list)
    barrier_resolutions: list[BarrierResolution] = field(default_factory=list)
    validator_anchors: list[ValidatorAnchor] = field(default_factory=list)

    @property
    def material_gap_count(self) -> int:
        if not self.discovery_complete:
            return max(1, len(self.failed_resource_ids))
        return len(self.failed_resource_ids)

    @property
    def complete(self) -> bool:
        return self.discovery_complete and not self.failed_resource_ids

    @property
    def attempt_ids(self) -> tuple[str, ...]:
        return tuple(self.discovery_attempt_ids + self.fetch_attempt_ids)


@dataclass(frozen=True, slots=True)
class ReconcileSelection:
    parent_run_id: str
    start_at: datetime
    target: Mapping[str, Any]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("acquisition 时间必须包含时区")
    return value.astimezone(timezone.utc)


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value))


def _work_position(
    kind: str,
    *,
    page: int | None = None,
    cursor: str | None = None,
    canonical_resource_id: str | None = None,
) -> str:
    return json.dumps(
        {
            "canonical_resource_id": canonical_resource_id,
            "cursor": cursor,
            "kind": kind,
            "page": page,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_work_position(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class _LeaseHeartbeat:
    """Keep one run lease alive without holding a transaction during I/O."""

    def __init__(
        self,
        repository: Any,
        run_id: str,
        owner_token: str,
        lease_epoch: int,
        *,
        ttl_seconds: int,
        interval_seconds: float,
        now: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.run_id = run_id
        self.owner_token = owner_token
        self.lease_epoch = lease_epoch
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = max(0.05, interval_seconds)
        self.now = now
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # The executor thread performs boundary renewals while the background
        # thread performs periodic renewals during blocking source I/O.  Keep
        # those writes serialized so an older wall-clock sample cannot race a
        # newer renewal and move ``expires_at`` backwards.
        self._renew_lock = threading.Lock()
        self._error: BaseException | None = None
        self._last_renewed = _utc(now())
        self._thread = threading.Thread(
            target=self._loop,
            name=f"acquisition-lease-{run_id[:12]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.renew(force=True)
            except Exception as exc:  # surfaced on the executor thread
                with self._lock:
                    self._error = exc
                self._stop.set()
                return

    def check(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise StaleLeaseError("lease heartbeat failed") from error

    def renew(self, *, force: bool = False) -> None:
        with self._renew_lock:
            self.check()
            current = _utc(self.now())
            if (
                not force
                and (current - self._last_renewed).total_seconds()
                < self.interval_seconds
            ):
                return
            self.repository.renew_lease(
                self.run_id,
                owner_token=self.owner_token,
                lease_epoch=self.lease_epoch,
                now=current,
                ttl_seconds=self.ttl_seconds,
            )
            self._last_renewed = current

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self.check()


class _RetainedParserAdapter:
    def __init__(self, adapter: Any, work: QueryWork) -> None:
        self.adapter = adapter
        self.work = work

    def parse_retained_discovery(self, snapshot_id: str) -> NormalizedDiscoveryPage:
        return _normalized_page(
            self.adapter.parse_retained_discovery(snapshot_id, self.work)
        )


class _NonRetainedParserAdapter:
    def __init__(
        self,
        adapter: Any,
        work: QueryWork,
        *,
        request_url: str,
        final_url: str,
        headers: Mapping[str, str],
        redirects: tuple[str, ...],
        observed_at: datetime,
        retrieved_at: datetime,
    ) -> None:
        self.adapter = adapter
        self.work = work
        self.request_url = request_url
        self.final_url = final_url
        self.headers = headers
        self.redirects = redirects
        self.observed_at = observed_at
        self.retrieved_at = retrieved_at

    def validate_and_normalize_without_retention(
        self, envelope: BoundedDiscoveryEnvelope
    ) -> NormalizedDiscoveryPage:
        body = envelope.read_once()
        typed = BoundedTransportEnvelope(
            request_url=self.request_url,
            final_url=self.final_url,
            status_code=envelope.status_code,
            headers=self.headers,
            body=body,
            redirect_chain=self.redirects,
            observed_at=self.observed_at,
            retrieved_at=self.retrieved_at,
            body_limit=max(1, self.work.max_response_bytes),
        )
        return _normalized_page(
            self.adapter.validate_and_normalize_without_retention(typed, self.work)
        )


def _normalized_page(result: AdapterDiscoveryResult | Any) -> NormalizedDiscoveryPage:
    resources = tuple(
        NormalizedResource(
            canonical_resource_id=row.canonical_resource_id,
            upstream_material_id=row.upstream_material_id,
            resource_url=row.url,
            title=row.title,
            published_at_raw=row.published_raw,
            published_at=row.published_at,
            published_at_precision=PublishedAtPrecision(row.published_at_precision),
            source_timezone=row.source_timezone,
            row_locator=row.row_locator,
            required_fetch=bool(row.required_fetch),
            expected_mime_types=tuple(
                row.metadata.get("expected_mime_types", ())
                if isinstance(row.metadata, Mapping)
                else ()
            ),
            metadata=(dict(row.metadata) if isinstance(row.metadata, Mapping) else {}),
        )
        for row in result.resources
    )
    return NormalizedDiscoveryPage(
        resources=resources,
        parser_id="source_adapter",
        parser_version=str(result.parser_schema_version),
        schema_id="source_discovery",
        schema_version=str(result.parser_schema_version),
        schema_valid=bool(result.schema_valid),
        declared_total=result.declared_total,
        declared_page_count=result.page_count,
        terminal=bool(result.terminal),
        next_cursor=result.next_cursor,
    )


class AcquisitionOrchestrator:
    """Execute one durable acquisition plan through append-only evidence state.

    The orchestrator is intentionally the only layer that joins registry
    policy, adapters, run leases, discovery proof, required fetches, coverage
    and conservative checkpoints.  Adapters remain stateless and never receive
    a data-root or repository.
    """

    def __init__(
        self,
        runtime: Any,
        *,
        transport_factory: Callable[[SourceDefinition], Any] | None = None,
        adapter_resolver: Callable[[SourceDefinition, Any], Any] | None = None,
        policy_guard: Callable[[SourceDefinition], bool] | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        lease_ttl_seconds: int = 60,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self.runtime = runtime
        self.repository = runtime.repository
        self.snapshot_service = runtime.snapshot_service
        self.manifest_service = getattr(runtime, "manifest_service", None)
        self.adapter_factory = runtime.adapter_factory
        self.source_gate = runtime.source_gate
        self._transport_factory = transport_factory
        self._adapter_resolver = adapter_resolver
        self._policy_guard = policy_guard
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self.lease_ttl_seconds = lease_ttl_seconds
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds必须为正数")
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or max(
            0.1, lease_ttl_seconds / 3
        )
        self.checkpoint_engine = CheckpointEngine()
        self.discovery_pipeline = DiscoveryPipeline(
            self.snapshot_service, self.repository
        )
        self._adapter_cache: dict[tuple[str, str], Any] = {}
        self._transport_cache: dict[tuple[str, str], Any] = {}

    # Planning -------------------------------------------------------------
    def persist_plan(self, plan: AcquisitionPlan) -> AcquisitionPlan:
        """Atomically persist the immutable run/coverage/physical-plan graph."""

        self.repository.save_plan_bundle(
            plan.run,
            plan.physical_query_plan_items,
            plan.coverage_entries,
            plan.coverage_links,
        )
        events = self.repository.list_run_events(plan.run.run_id)
        if not any(event.event_type == AcquisitionRunEventType.PLANNED for event in events):
            self.repository.append_run_event(
                AcquisitionRunEvent(
                    run_id=plan.run.run_id,
                    event_type=AcquisitionRunEventType.PLANNED,
                    occurred_at=plan.run.created_at,
                    metadata={
                        "physical_query_count": len(plan.physical_query_plan_items),
                        "coverage_entry_count": len(plan.coverage_entries),
                        "coverage_link_count": len(plan.coverage_links),
                    },
                )
            )
        return plan

    def plan(self, *args: Any, **kwargs: Any) -> AcquisitionPlan:
        plan = self.runtime.planner.plan(*args, **kwargs)
        return self.persist_plan(plan)

    def smoke_sources(
        self,
        *,
        ticker: str,
        source_ids: Sequence[str] | None = None,
        as_of: datetime | None = None,
        owner_token: str | None = None,
        lease_ttl_seconds: int | None = None,
    ) -> AcquisitionExecutionResult:
        """Create and execute one durable, registry-defined smoke plan.

        A smoke run uses only queries explicitly marked ``smoke_enabled`` and
        a bounded one-day range.  Disabled or market-inapplicable selected
        sources remain visible as static coverage entries and therefore cause
        zero network I/O rather than being silently dropped.
        """

        definitions = tuple(
            item
            for item in self._definitions()
            if "business_model" in item.scopes and item.scope_version == "v1"
        )
        by_id = {item.source_definition_id: item for item in definitions}
        defaults = tuple(
            item.source_definition_id
            for item in definitions
            if any(query.smoke_enabled for query in item.queries)
        )
        selected_ids = tuple(dict.fromkeys(source_ids or defaults))
        unknown = sorted(set(selected_ids) - set(by_id))
        if unknown:
            raise ValueError(f"未注册的smoke来源: {', '.join(unknown)}")
        if not selected_ids:
            raise ValueError("注册表没有smoke_enabled查询")
        smoke_queries = {
            definition_id: {
                query.query_id
                for query in by_id[definition_id].queries
                if query.smoke_enabled
            }
            for definition_id in selected_ids
        }
        missing = sorted(
            definition_id
            for definition_id, query_ids in smoke_queries.items()
            if not query_ids
        )
        if missing:
            raise ValueError(
                f"所选来源没有smoke_enabled查询: {', '.join(missing)}"
            )

        cutoff = _utc(as_of or self._now())
        profile = self.runtime.build_profile(ticker)
        base = self.runtime.planner.plan(
            profile,
            mode=AcquisitionMode.INCREMENTAL,
            as_of=cutoff,
            start_at=cutoff - timedelta(days=1),
            run_kind=AcquisitionRunKind.SMOKE,
            storage_namespace_id=str(getattr(self.runtime, "namespace_id", "")) or None,
        )
        plans = tuple(
            item
            for item in base.physical_query_plan_items
            if item.source_definition_id in smoke_queries
            and item.query_id in smoke_queries[item.source_definition_id]
        )
        plan_ids = {item.plan_item_id for item in plans}
        links = tuple(
            item for item in base.coverage_links if item.plan_item_id in plan_ids
        )
        linked_coverage_ids = {item.coverage_entry_id for item in links}
        coverage = tuple(
            item
            for item in base.coverage_entries
            if item.coverage_entry_id in linked_coverage_ids
            or (
                item.source_definition_id in smoke_queries
                and item.query_id in smoke_queries[item.source_definition_id]
                and item.plan_disposition
                == CoveragePlanDisposition.STATIC_POLICY_SKIPPED
            )
        )
        selected_refs = tuple(
            item
            for item in base.run.source_definition_refs
            if item.source_definition_id in selected_ids
        )
        run = AcquisitionRun.model_validate(
            {
                **base.run.model_dump(mode="python"),
                "source_definition_refs": selected_refs,
            }
        )
        plan = AcquisitionPlan(
            run=run,
            coverage_entries=coverage,
            physical_query_plan_items=plans,
            coverage_links=links,
        )
        return self.execute_plan(
            plan,
            owner_token=owner_token,
            lease_ttl_seconds=lease_ttl_seconds,
        )

    execute_smoke = smoke_sources

    def execute_plan(
        self,
        plan: AcquisitionPlan,
        *,
        owner_token: str | None = None,
        lease_ttl_seconds: int | None = None,
    ) -> AcquisitionExecutionResult:
        self.persist_plan(plan)
        return self.execute_run(
            plan.run.run_id,
            owner_token=owner_token,
            lease_ttl_seconds=lease_ttl_seconds,
        )

    # Execution ------------------------------------------------------------
    def execute_run(
        self,
        run_id: str,
        *,
        owner_token: str | None = None,
        lease_ttl_seconds: int | None = None,
    ) -> AcquisitionExecutionResult:
        effective_ttl = (
            self.lease_ttl_seconds
            if lease_ttl_seconds is None
            else int(lease_ttl_seconds)
        )
        if effective_ttl <= 0:
            raise ValueError("lease_ttl_seconds必须为正数")
        run = self.repository.get_run(run_id)
        self._assert_namespace(run)
        existing_final = self._final_event(run_id)
        if existing_final is not None:
            return self._result_from_final(run, existing_final)
        for definition in self._frozen_definitions_for_run(run):
            try:
                SourceRegistryLoader.assert_effective(definition, run.as_of)
            except SourceRegistryError as exc:
                raise AcquisitionExecutionError(
                    f"run固定的来源定义对run as_of无效: "
                    f"{definition.source_definition_id}@{definition.version}: {exc}"
                ) from exc
        plan_items = self.repository.list_physical_query_plan_items(run_id)
        coverage_entries = self.repository.list_coverage_entries(run_id)
        coverage_links = self.repository.list_physical_query_coverage_links(run_id=run_id)
        self._validate_durable_plan(run, plan_items, coverage_entries, coverage_links)

        lease, token = self.repository.claim_lease(
            run_id,
            owner_token=owner_token,
            now=_utc(self._now()),
            ttl_seconds=effective_ttl,
        )
        heartbeat = _LeaseHeartbeat(
            self.repository,
            run_id,
            token,
            lease.lease_epoch,
            ttl_seconds=effective_ttl,
            interval_seconds=min(
                self.heartbeat_interval_seconds,
                max(0.1, effective_ttl / 3),
            ),
            now=self._now,
        )
        heartbeat.start()
        released = False
        try:
            # Establish a fresh lease boundary before the first fenced write.
            # The periodic thread then keeps it alive while a source gate or
            # network request blocks the executor thread.
            heartbeat.renew(force=True)
            self.repository.append_run_event(
                AcquisitionRunEvent(
                    run_id=run_id,
                    event_type=AcquisitionRunEventType.RUNNING,
                    occurred_at=_utc(self._now()),
                    lease_epoch=lease.lease_epoch,
                    metadata={"reclaimed": lease.lease_epoch > 1},
                ),
                owner_token=token,
            )
            resume = self._close_abandoned_attempts(
                run, lease.lease_epoch, token
            )
            executions: dict[str, _PlanExecution] = {}
            for item in sorted(plan_items, key=lambda value: (value.ordinal, value.plan_item_id)):
                if item.attempt_kind != AttemptKind.DISCOVERY or item.parent_plan_item_id:
                    continue
                heartbeat.renew(force=True)
                executions[item.plan_item_id] = self._execute_discovery_plan(
                    run,
                    item,
                    lease_epoch=lease.lease_epoch,
                    owner_token=token,
                    heartbeat=heartbeat,
                    resume_attempt=resume.get(item.plan_item_id),
                )
            heartbeat.renew(force=True)
            result = self._finalize(
                run,
                executions,
                coverage_entries,
                coverage_links,
                lease_epoch=lease.lease_epoch,
                owner_token=token,
                heartbeat=heartbeat,
            )
            heartbeat.renew(force=True)
            heartbeat.stop()
            self.repository.release_lease(
                run_id,
                owner_token=token,
                lease_epoch=lease.lease_epoch,
                now=_utc(self._now()),
            )
            released = True
            return result
        finally:
            try:
                heartbeat.stop()
            except StaleLeaseError:
                pass
            if not released:
                try:
                    self.repository.release_lease(
                        run_id,
                        owner_token=token,
                        lease_epoch=lease.lease_epoch,
                        now=_utc(self._now()),
                    )
                except (StaleLeaseError, LeaseConflictError):
                    pass
            self._close_transports()

    execute = execute_run

    def _assert_namespace(self, run: AcquisitionRun) -> None:
        runtime_namespace = str(getattr(self.runtime, "namespace_id", ""))
        if run.storage_namespace_id and run.storage_namespace_id != runtime_namespace:
            raise AcquisitionNamespaceMismatch(
                "run与当前database/data_root storage namespace不匹配"
            )

    def _validate_durable_plan(
        self,
        run: AcquisitionRun,
        plan_items: Sequence[PhysicalQueryPlanItem],
        coverage_entries: Sequence[CoverageEntry],
        links: Sequence[PhysicalQueryCoverageLink],
    ) -> None:
        if not coverage_entries:
            raise AcquisitionExecutionError("run没有durable coverage matrix")
        plan_ids = {item.plan_item_id for item in plan_items}
        coverage_ids = {item.coverage_entry_id for item in coverage_entries}
        for link in links:
            if link.plan_item_id not in plan_ids or link.coverage_entry_id not in coverage_ids:
                raise AcquisitionExecutionError("durable plan-to-coverage link悬空")
        linked = {item.coverage_entry_id for item in links}
        missing = [
            item.coverage_entry_id
            for item in coverage_entries
            if item.plan_disposition == CoveragePlanDisposition.REQUIRED
            and item.coverage_entry_id not in linked
        ]
        if missing:
            raise AcquisitionExecutionError(
                f"required coverage缺少durable physical plan link: {sorted(missing)}"
            )
        if any(item.run_id != run.run_id for item in (*plan_items, *coverage_entries)):
            raise AcquisitionExecutionError("plan graph引用了其他run")

    def _close_abandoned_attempts(
        self,
        run: AcquisitionRun,
        lease_epoch: int,
        owner_token: str,
    ) -> dict[str, AcquisitionAttempt]:
        resumable: dict[str, AcquisitionAttempt] = {}
        for attempt in self.repository.list_attempts(run_id=run.run_id):
            events = self.repository.list_attempt_events(attempt.attempt_id)
            terminal = any(
                event.event_type
                in {
                    AcquisitionAttemptEventType.OUTCOME_TERMINAL,
                    AcquisitionAttemptEventType.ABANDONED,
                }
                for event in events
            )
            if terminal:
                continue
            self.repository.append_attempt_event(
                AcquisitionAttemptEvent(
                    attempt_id=attempt.attempt_id,
                    event_type=AcquisitionAttemptEventType.ABANDONED,
                    occurred_at=_utc(self._now()),
                    lease_epoch=lease_epoch,
                    reason_code="execution_lease_expired_before_protocol_result",
                    protocol_summary={
                        "prior_lease_epoch": attempt.lease_epoch,
                        "reclaimed_by_epoch": lease_epoch,
                    },
                ),
                owner_token=owner_token,
            )
            current = resumable.get(attempt.physical_query_plan_item_id)
            if current is None or attempt.retry_ordinal > current.retry_ordinal:
                resumable[attempt.physical_query_plan_item_id] = attempt
        return resumable

    def _execute_discovery_plan(
        self,
        run: AcquisitionRun,
        plan_item: PhysicalQueryPlanItem,
        *,
        lease_epoch: int,
        owner_token: str,
        heartbeat: _LeaseHeartbeat,
        resume_attempt: AcquisitionAttempt | None,
    ) -> _PlanExecution:
        definition = self._source_definition_for_run(
            run,
            plan_item.source_definition_id,
            plan_item.source_definition_version,
        )
        query = self._query_definition(definition, plan_item.query_id)
        execution = _PlanExecution(plan_item=plan_item)
        self._load_existing_discovery(execution)
        if execution.discovery_complete:
            adapter = self._adapter_for(definition)
            self._execute_required_fetches(
                run,
                definition,
                query,
                adapter,
                execution,
                lease_epoch=lease_epoch,
                owner_token=owner_token,
                heartbeat=heartbeat,
            )
            return execution

        policy_reason = self._runtime_policy_reason(definition)
        start_page, start_cursor = self._resume_position(
            query, resume_attempt
        )
        prior_barriers, prior_attempt = self._prior_unresolved_barriers(
            plan_item,
            attempt_kind=AttemptKind.DISCOVERY,
        )
        previous = (
            resume_attempt
            or self._latest_attempt(plan_item.plan_item_id)
            or prior_attempt
        )
        retry_group_id = (
            previous.retry_group_id
            if previous is not None
            else stable_acquisition_id(
                "retry-group",
                {"run_id": run.run_id, "plan_item_id": plan_item.plan_item_id},
            )
        )
        retry_ordinal = -1 if previous is None else previous.retry_ordinal
        deadline = self._monotonic() + float(
            definition.retry_policy.attempt_deadline_seconds
        )
        pending_barriers: list[BarrierDraft] = list(prior_barriers)
        attempts_made = 0

        while True:
            heartbeat.renew(force=True)
            retry_ordinal += 1
            attempts_made += 1
            work_position = _work_position(
                "discovery", page=start_page, cursor=start_cursor
            )
            attempt = self._start_attempt(
                run,
                plan_item,
                attempt_kind=AttemptKind.DISCOVERY,
                lease_epoch=lease_epoch,
                owner_token=owner_token,
                work_position=work_position,
                retry_group_id=retry_group_id,
                retry_ordinal=retry_ordinal,
                page=start_page,
                cursor=start_cursor,
                supersedes=previous,
            )
            execution.discovery_attempt_ids.append(attempt.attempt_id)
            if policy_reason is not None:
                fact = self._terminal(
                    attempt,
                    AttemptClassification(
                        AcquisitionOutcome.POLICY_SKIPPED, policy_reason
                    ),
                    owner_token=owner_token,
                )
                barrier = barrier_for_attempt(
                    attempt=attempt,
                    plan_item=plan_item,
                    work_position=work_position,
                    outcome=fact.outcome,
                    reason_code=fact.reason_code,
                )
                execution.barriers.append(barrier)
                execution.reasons.append(fact.reason_code)
                return execution

            adapter = self._adapter_for(definition)
            fact, next_page, next_cursor, retry_delay = self._run_discovery_attempt(
                run,
                definition,
                query,
                adapter,
                plan_item,
                attempt,
                start_page=start_page,
                start_cursor=start_cursor,
                deadline=deadline,
                lease_epoch=lease_epoch,
                owner_token=owner_token,
                heartbeat=heartbeat,
                execution=execution,
            )
            if fact.outcome in {
                AcquisitionOutcome.SUCCESS,
                AcquisitionOutcome.NO_DATA,
                AcquisitionOutcome.UNCHANGED,
            }:
                execution.discovery_complete = True
                execution.discovery_outcome = fact.outcome
                for barrier in pending_barriers:
                    proof_id = self._proof_for_barrier(attempt, barrier)
                    if proof_id is None:
                        continue
                    execution.barrier_resolutions.append(
                        BarrierResolution(
                            barrier_id=barrier.barrier_id,
                            opening_attempt_id=barrier.opening_attempt_id,
                            resolving_attempt_id=attempt.attempt_id,
                            source_definition_id=barrier.source_definition_id,
                            source_definition_version=barrier.source_definition_version,
                            partition_key=barrier.partition_key,
                            work_position=barrier.work_position,
                            retry_group_id=barrier.retry_group_id,
                            canonical_resource_id=barrier.canonical_resource_id,
                            discovery_proof_id=proof_id,
                            lease_epoch=lease_epoch,
                            created_at=_utc(self._now()),
                        )
                    )
                break

            barrier = barrier_for_attempt(
                attempt=attempt,
                plan_item=plan_item,
                work_position=fact.work_position or work_position,
                outcome=fact.outcome,
                reason_code=fact.reason_code,
            )
            execution.barriers.append(barrier)
            pending_barriers.append(barrier)
            execution.reasons.append(fact.reason_code)
            if retry_delay is None:
                return execution
            if attempts_made >= int(definition.retry_policy.max_attempts):
                return execution
            if self._monotonic() + retry_delay >= deadline:
                return execution
            if retry_delay:
                self._sleep(retry_delay)
            previous = attempt
            start_page = next_page
            start_cursor = next_cursor

        self._load_existing_discovery(execution)
        self._execute_required_fetches(
            run,
            definition,
            query,
            adapter,
            execution,
            lease_epoch=lease_epoch,
            owner_token=owner_token,
            heartbeat=heartbeat,
        )
        return execution

    def _run_discovery_attempt(
        self,
        run: AcquisitionRun,
        definition: SourceDefinition,
        query: SourceQueryDefinition,
        adapter: Any,
        plan_item: PhysicalQueryPlanItem,
        attempt: AcquisitionAttempt,
        *,
        start_page: int,
        start_cursor: str | None,
        deadline: float,
        lease_epoch: int,
        owner_token: str,
        heartbeat: _LeaseHeartbeat,
        execution: _PlanExecution,
    ) -> tuple[_AttemptFact, int, str | None, float | None]:
        page = start_page
        cursor = start_cursor
        seen_cursors: set[str] = set()
        committed = 0
        attempt_proofs: list[str] = []
        attempt_snapshots: list[str] = []
        while True:
            heartbeat.renew()
            position = _work_position("discovery", page=page, cursor=cursor)
            try:
                # Building the wire request can depend on an earlier persisted
                # discovery proof (for example CNINFO's company bootstrap
                # ``orgId`` binding).  A missing or ambiguous binding is a
                # classified discovery failure for this already-started
                # attempt, not an executor crash that leaves it unterminated.
                work = self._query_work(
                    definition,
                    query,
                    plan_item,
                    run=run,
                    attempt=attempt,
                    heartbeat=heartbeat,
                    page=page,
                    cursor=cursor,
                    deadline=deadline,
                )
                envelope = adapter.execute_query(work)
                # Source-gate waits and the request itself can consume most of
                # a short TTL.  Refresh before any response-derived evidence is
                # committed, even if the periodic thread already renewed it.
                heartbeat.renew(force=True)
                response_classification = classify_response(
                    status_code=envelope.status_code,
                    headers=envelope.headers,
                    body_prefix=envelope.body,
                    expected_mime=tuple(query.discovery_schema.response_mime_types),
                    has_committed_segments=committed > 0,
                )
                if response_classification.outcome != AcquisitionOutcome.SUCCESS:
                    self._record_failed_discovery_observation(
                        attempt,
                        plan_item,
                        definition,
                        envelope,
                        owner_token=owner_token,
                        lease_epoch=lease_epoch,
                    )
                    fact = self._terminal(
                        attempt,
                        response_classification,
                        owner_token=owner_token,
                        proof_ids=attempt_proofs,
                        snapshot_ids=attempt_snapshots,
                        work_position=position,
                    )
                    delay = self._retry_delay(
                        response_classification,
                        envelope.headers,
                        definition,
                        deadline,
                    )
                    return fact, page, cursor, delay

                page_result = self._process_discovery_page(
                    definition,
                    query,
                    adapter,
                    work,
                    plan_item,
                    attempt,
                    envelope,
                    owner_token=owner_token,
                    lease_epoch=lease_epoch,
                )
                attempt_proofs.append(page_result.proof.proof_id)
                execution.proof_ids.append(page_result.proof.proof_id)
                if page_result.discovery_snapshot_id:
                    attempt_snapshots.append(page_result.discovery_snapshot_id)
                    execution.discovery_snapshot_ids.append(
                        page_result.discovery_snapshot_id
                    )
                for resource in page_result.resources:
                    execution.resources.setdefault(
                        resource.canonical_resource_id, resource
                    )
                next_page, next_cursor = self._next_page(
                    query,
                    page_result.proof,
                    page,
                    cursor,
                    adapter_next_cursor=page_result.next_cursor,
                )
                segment = AcquisitionAttemptSegment(
                    attempt_id=attempt.attempt_id,
                    segment_ordinal=committed,
                    page_number=page,
                    cursor=cursor,
                    work_position=position,
                    lease_epoch=lease_epoch,
                    committed_at=_utc(self._now()),
                    discovery_observation_id=page_result.observation.observation_id,
                    discovery_proof_id=page_result.proof.proof_id,
                    snapshot_ids=(
                        ()
                        if page_result.discovery_snapshot_id is None
                        else (page_result.discovery_snapshot_id,)
                    ),
                    next_safe_position=(
                        None
                        if page_result.proof.terminal
                        else _work_position(
                            "discovery", page=next_page, cursor=next_cursor
                        )
                    ),
                )
                self.repository.append_attempt_segment(
                    segment, owner_token=owner_token
                )
                self.repository.append_attempt_event(
                    AcquisitionAttemptEvent(
                        attempt_id=attempt.attempt_id,
                        event_type=AcquisitionAttemptEventType.SEGMENT_COMMITTED,
                        occurred_at=_utc(self._now()),
                        lease_epoch=lease_epoch,
                        segment_id=segment.segment_id,
                        proof_ids=(page_result.proof.proof_id,),
                        snapshot_ids=segment.snapshot_ids,
                        protocol_summary={
                            "page": page,
                            "cursor": cursor,
                            "terminal": page_result.proof.terminal,
                        },
                    ),
                    owner_token=owner_token,
                )
                committed += 1
                if page_result.proof.terminal:
                    completion = self._proof_completion(plan_item.plan_item_id)
                    classification = classify_discovery_completion(
                        normalized_resource_count=completion[0],
                        schema_valid=True,
                        proof_complete=True,
                        pagination_terminal=True,
                        declared_total=completion[1],
                        normalized_total=completion[0],
                        has_committed_segments=committed > 0,
                    )
                    fact = self._terminal(
                        attempt,
                        classification,
                        owner_token=owner_token,
                        proof_ids=attempt_proofs,
                        snapshot_ids=attempt_snapshots,
                        work_position=position,
                    )
                    return fact, next_page, next_cursor, None
                if next_cursor and next_cursor in seen_cursors:
                    raise DiscoveryValidationError(
                        "duplicate_cursor", "discovery cursor重复，无法证明终止"
                    )
                if next_cursor:
                    seen_cursors.add(next_cursor)
                page, cursor = next_page, next_cursor
            except StaleLeaseError:
                raise
            except Exception as exc:
                classification = self._classify_error(
                    exc,
                    has_committed_segments=committed > 0,
                    deadline=deadline,
                )
                fact = self._terminal(
                    attempt,
                    classification,
                    owner_token=owner_token,
                    proof_ids=attempt_proofs,
                    snapshot_ids=attempt_snapshots,
                    work_position=position,
                )
                delay = self._retry_delay(
                    classification, {}, definition, deadline
                )
                return fact, page, cursor, delay

    def _process_discovery_page(
        self,
        definition: SourceDefinition,
        query: SourceQueryDefinition,
        adapter: Any,
        work: QueryWork,
        plan_item: PhysicalQueryPlanItem,
        attempt: AcquisitionAttempt,
        envelope: BoundedTransportEnvelope,
        *,
        owner_token: str,
        lease_epoch: int,
    ) -> Any:
        context = DiscoveryPageContext(
            attempt_id=attempt.attempt_id,
            physical_query_plan_item_id=plan_item.plan_item_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            page_number=work.page,
            cursor=work.cursor,
            observed_at=envelope.observed_at,
            retrieved_at=envelope.retrieved_at,
            http_status=envelope.status_code,
            mime_type=envelope.content_type
            or query.discovery_schema.response_mime_types[0],
            request_summary={"method": work.method, "url": envelope.request_url},
            response_summary={
                "headers": sanitize_http_metadata(envelope.headers),
                "final_url": envelope.final_url,
                "redirect_chain": list(envelope.redirect_chain),
            },
        )
        query_page_canonical = stable_acquisition_id(
            "query-page",
            {
                "plan_item_id": plan_item.plan_item_id,
                "page": work.page,
                "cursor": work.cursor,
            },
        )
        if query.discovery_body_policy == DiscoveryBodyPolicy.RETAIN:
            return self.discovery_pipeline.process_retained(
                body=envelope.body,
                context=context,
                query_page_canonical=query_page_canonical,
                parser=_RetainedParserAdapter(adapter, work),
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
        if query.discovery_body_policy == DiscoveryBodyPolicy.MINIMAL_PROOF:
            bounded = BoundedDiscoveryEnvelope(
                envelope.body,
                max_bytes=work.max_response_bytes,
                status_code=envelope.status_code,
                mime_type=context.mime_type,
                headers=envelope.headers,
            )
            validator = _NonRetainedParserAdapter(
                adapter,
                work,
                request_url=envelope.request_url,
                final_url=envelope.final_url,
                headers=envelope.headers,
                redirects=envelope.redirect_chain,
                observed_at=envelope.observed_at,
                retrieved_at=envelope.retrieved_at,
            )
            return self.discovery_pipeline.process_without_retention(
                envelope=bounded,
                context=context,
                validator=validator,
                independent_replay_required=definition.retention_policy.independent_replay_required,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
        raise SecurityPolicyError(
            "license_not_approved", "discovery body策略禁止执行该查询"
        )

    def _execute_required_fetches(
        self,
        run: AcquisitionRun,
        definition: SourceDefinition,
        query: SourceQueryDefinition,
        adapter: Any,
        execution: _PlanExecution,
        *,
        lease_epoch: int,
        owner_token: str,
        heartbeat: _LeaseHeartbeat,
    ) -> None:
        required = [
            item
            for item in execution.resources.values()
            if item.required_fetch
        ]
        for index, resource in enumerate(
            sorted(required, key=lambda item: item.canonical_resource_id)
        ):
            if self._resource_completed_in_run(run.run_id, resource.canonical_resource_id):
                self._load_resource_result(execution, run.run_id, resource)
                continue
            fetch_plan = self._ensure_fetch_plan(
                execution.plan_item, resource, index
            )
            self._execute_fetch(
                run,
                definition,
                query,
                adapter,
                fetch_plan,
                resource,
                execution,
                lease_epoch=lease_epoch,
                owner_token=owner_token,
                heartbeat=heartbeat,
            )

    def _execute_fetch(
        self,
        run: AcquisitionRun,
        definition: SourceDefinition,
        query: SourceQueryDefinition,
        adapter: Any,
        fetch_plan: PhysicalQueryPlanItem,
        resource: DiscoveredResource,
        execution: _PlanExecution,
        *,
        lease_epoch: int,
        owner_token: str,
        heartbeat: _LeaseHeartbeat,
    ) -> None:
        prior_barriers, prior_attempt = self._prior_unresolved_barriers(
            fetch_plan,
            attempt_kind=AttemptKind.FETCH,
            canonical_resource_id=resource.canonical_resource_id,
        )
        previous = self._latest_attempt(fetch_plan.plan_item_id) or prior_attempt
        retry_group_id = (
            previous.retry_group_id
            if previous is not None
            else stable_acquisition_id(
                "retry-group",
                {
                    "run_id": run.run_id,
                    "plan_item_id": fetch_plan.plan_item_id,
                    "canonical_resource_id": resource.canonical_resource_id,
                },
            )
        )
        ordinal = -1 if previous is None else previous.retry_ordinal
        deadline = self._monotonic() + float(
            definition.retry_policy.attempt_deadline_seconds
        )
        force_unconditional = False
        pending_barriers: list[BarrierDraft] = list(prior_barriers)
        attempts_made = 0
        while True:
            heartbeat.renew(force=True)
            ordinal += 1
            attempts_made += 1
            position = _work_position(
                "fetch", canonical_resource_id=resource.canonical_resource_id
            )
            parent_attempt_id = self._parent_discovery_attempt_id(execution)
            attempt = self._start_attempt(
                run,
                fetch_plan,
                attempt_kind=AttemptKind.FETCH,
                lease_epoch=lease_epoch,
                owner_token=owner_token,
                work_position=position,
                retry_group_id=retry_group_id,
                retry_ordinal=ordinal,
                resource=resource,
                parent_discovery_attempt_id=parent_attempt_id,
                supersedes=previous,
            )
            execution.fetch_attempt_ids.append(attempt.attempt_id)
            anchor, validators = self._validator_anchor(
                definition, resource, force_unconditional=force_unconditional
            )
            query_work = self._query_work(
                definition,
                query,
                fetch_plan,
                run=run,
                attempt=attempt,
                heartbeat=heartbeat,
                page=1,
                cursor=None,
                deadline=deadline,
                url=resource.resource_url,
            )
            adapter_resource = AdapterNormalizedResource(
                canonical_resource_id=resource.canonical_resource_id,
                upstream_material_id=resource.upstream_material_id
                or resource.canonical_resource_id,
                title=resource.title,
                url=resource.resource_url,
                published_raw=resource.published_at_raw,
                published_at=resource.published_at,
                published_at_precision=resource.published_at_precision.value,
                source_timezone=resource.source_timezone,
                row_locator=resource.row_locator or resource.row_hash,
                row_hash=resource.row_hash,
                required_fetch=True,
                metadata={"expected_mime_types": resource.expected_mime_types},
            )
            try:
                heartbeat.renew(force=True)
                envelope = adapter.fetch_resource(
                    FetchWork(
                        query=query_work,
                        resource=adapter_resource,
                        validators=validators,
                    )
                )
                heartbeat.renew(force=True)
                if envelope.status_code == 304:
                    classification, _ = classify_fetch_result(
                        status_code=304,
                        existing_snapshot_valid=anchor is not None,
                    )
                    if anchor is None and self._has_snapshot_candidate(
                        definition, resource
                    ):
                        classification = AttemptClassification(
                            AcquisitionOutcome.PARSE_FAILED, "invalid_304"
                        )
                    if anchor is not None:
                        observation = ResourceObservation(
                            attempt_id=attempt.attempt_id,
                            discovered_resource_id=resource.discovered_resource_id,
                            parent_discovery_attempt_id=parent_attempt_id,
                            source_definition_id=definition.source_definition_id,
                            source_definition_version=definition.version,
                            snapshot_id=anchor.snapshot_id,
                            disposition=ResourceDisposition.UNCHANGED,
                            attempt_outcome=AcquisitionOutcome.UNCHANGED,
                            original_url=resource.resource_url,
                            final_url=envelope.final_url,
                            redirect_chain=tuple(
                                {"url": item} for item in envelope.redirect_chain
                            ),
                            request_summary={"conditional": True},
                            response_summary={"headers": dict(envelope.headers)},
                            http_status=304,
                            etag=envelope.headers.get("etag") or anchor.etag,
                            last_modified=envelope.headers.get("last-modified")
                            or anchor.last_modified,
                            validator_source_snapshot_id=anchor.snapshot_id,
                            observed_at=envelope.observed_at,
                            retrieved_at=envelope.retrieved_at,
                            reason_code=classification.reason_code,
                        )
                        self.repository.save_resource_observation(
                            observation,
                            owner_token=owner_token,
                            lease_epoch=lease_epoch,
                        )
                        fact = self._terminal(
                            attempt,
                            classification,
                            owner_token=owner_token,
                            snapshot_ids=(anchor.snapshot_id,),
                            resource_observation_ids=(observation.observation_id,),
                            work_position=position,
                        )
                        execution.content_snapshot_ids.append(anchor.snapshot_id)
                        execution.resource_observation_ids.append(
                            observation.observation_id
                        )
                        execution.validator_anchors.append(
                            ValidatorAnchor(
                                canonical_resource_id=resource.canonical_resource_id,
                                resource_url=resource.resource_url,
                                snapshot_id=anchor.snapshot_id,
                                etag=observation.etag,
                                last_modified=observation.last_modified,
                                observed_at=observation.observed_at,
                            )
                        )
                        self._resolve_pending_fetch_barriers(
                            pending_barriers,
                            attempt,
                            fact,
                            execution,
                            lease_epoch,
                        )
                        execution.failed_resource_ids.discard(
                            resource.canonical_resource_id
                        )
                        return
                    fact = self._terminal(
                        attempt,
                        classification,
                        owner_token=owner_token,
                        work_position=position,
                    )
                    barrier = barrier_for_attempt(
                        attempt=attempt,
                        plan_item=fetch_plan,
                        work_position=position,
                        outcome=fact.outcome,
                        reason_code=fact.reason_code,
                        canonical_resource_id=resource.canonical_resource_id,
                    )
                    execution.barriers.append(barrier)
                    pending_barriers.append(barrier)
                    execution.reasons.append(fact.reason_code)
                    execution.failed_resource_ids.add(resource.canonical_resource_id)
                    # Invalid 304 always receives one independent unconditional
                    # fetch attempt, even when the ordinary retry budget is one.
                    if force_unconditional:
                        return
                    force_unconditional = True
                    previous = attempt
                    continue

                response_classification = classify_response(
                    status_code=envelope.status_code,
                    headers=envelope.headers,
                    body_prefix=envelope.body,
                    expected_mime=tuple(resource.expected_mime_types)
                    or ("application/pdf", "text/html"),
                )
                if response_classification.outcome != AcquisitionOutcome.SUCCESS:
                    observation = self._failed_resource_observation(
                        attempt,
                        resource,
                        parent_attempt_id,
                        definition,
                        response_classification,
                        envelope=envelope,
                    )
                    self.repository.save_resource_observation(
                        observation,
                        owner_token=owner_token,
                        lease_epoch=lease_epoch,
                    )
                    fact = self._terminal(
                        attempt,
                        response_classification,
                        owner_token=owner_token,
                        resource_observation_ids=(observation.observation_id,),
                        work_position=position,
                    )
                    retry_delay = self._retry_delay(
                        response_classification,
                        envelope.headers,
                        definition,
                        deadline,
                    )
                else:
                    existing_sha = None if anchor is None else anchor.snapshot_id
                    anchor_snapshot = (
                        None
                        if anchor is None
                        else self.repository.get_raw_resource_snapshot(
                            anchor.snapshot_id
                        )
                    )
                    existing_content_sha = (
                        None if anchor_snapshot is None else anchor_snapshot.sha256
                    )
                    calculated = hashlib.sha256(envelope.body).hexdigest()
                    fetch_classification, disposition = classify_fetch_result(
                        status_code=envelope.status_code,
                        existing_snapshot_valid=anchor is not None,
                        response_sha256=calculated,
                        existing_sha256=existing_content_sha,
                    )
                    published_value: datetime | date | None = resource.published_at
                    if (
                        resource.published_at_precision == PublishedAtPrecision.DATE
                        and resource.published_at_raw
                    ):
                        try:
                            published_value = date.fromisoformat(
                                resource.published_at_raw[:10]
                            )
                        except ValueError:
                            published_value = resource.published_at
                    same_validator_changed_hash = bool(
                        anchor
                        and existing_content_sha
                        and calculated != existing_content_sha
                        and (
                            (
                                anchor.etag
                                and envelope.headers.get("etag") == anchor.etag
                            )
                            or (
                                anchor.last_modified
                                and envelope.headers.get("last-modified")
                                == anchor.last_modified
                            )
                        )
                    )
                    reason = (
                        "validator_hash_conflict_content_changed"
                        if same_validator_changed_hash
                        else fetch_classification.reason_code
                    )
                    frozen = self.snapshot_service.freeze_content(
                        envelope.body,
                        ContentSnapshotRequest(
                            attempt_id=attempt.attempt_id,
                            discovered_resource_id=resource.discovered_resource_id,
                            parent_discovery_attempt_id=parent_attempt_id,
                            source_definition_id=definition.source_definition_id,
                            source_definition_version=definition.version,
                            canonical_resource_id=resource.canonical_resource_id,
                            upstream_material_id=resource.upstream_material_id
                            or resource.canonical_resource_id,
                            canonical_url=resource.resource_url,
                            mime_type=envelope.content_type
                            or (resource.expected_mime_types[0] if resource.expected_mime_types else "application/octet-stream"),
                            observed_at=envelope.observed_at,
                            retrieved_at=envelope.retrieved_at,
                            published_at_raw=resource.published_at_raw,
                            published_at=published_value,
                            published_at_precision=resource.published_at_precision,
                            source_timezone=resource.source_timezone,
                            immutable_version_proven=resource.published_at_precision
                            != PublishedAtPrecision.UNKNOWN,
                            original_url=resource.resource_url,
                            final_url=envelope.final_url,
                            redirect_chain=envelope.redirect_chain,
                            request_summary={
                                "conditional": bool(validators),
                                "validator_source_snapshot_id": (
                                    None if anchor is None else anchor.snapshot_id
                                ),
                            },
                            response_summary={"headers": dict(envelope.headers)},
                            http_status=envelope.status_code,
                            etag=envelope.headers.get("etag"),
                            last_modified=envelope.headers.get("last-modified"),
                            validator_source_snapshot_id=(
                                None if anchor is None else anchor.snapshot_id
                            ),
                            reason_code=reason,
                        ),
                        owner_token=owner_token,
                        lease_epoch=lease_epoch,
                    )
                    outcome = (
                        AcquisitionOutcome.UNCHANGED
                        if frozen.disposition == ResourceDisposition.UNCHANGED.value
                        else AcquisitionOutcome.SUCCESS
                    )
                    fact = self._terminal(
                        attempt,
                        AttemptClassification(outcome, reason),
                        owner_token=owner_token,
                        snapshot_ids=(frozen.snapshot.snapshot_id,),
                        resource_observation_ids=(
                            frozen.observation.observation_id,
                        ),
                        work_position=position,
                    )
                    execution.content_snapshot_ids.append(
                        frozen.snapshot.snapshot_id
                    )
                    execution.resource_observation_ids.append(
                        frozen.observation.observation_id
                    )
                    etag = envelope.headers.get("etag")
                    last_modified = envelope.headers.get("last-modified")
                    if etag or last_modified:
                        execution.validator_anchors.append(
                            ValidatorAnchor(
                                canonical_resource_id=resource.canonical_resource_id,
                                resource_url=resource.resource_url,
                                snapshot_id=frozen.snapshot.snapshot_id,
                                etag=etag,
                                last_modified=last_modified,
                                observed_at=envelope.observed_at,
                            )
                        )
                    self._resolve_pending_fetch_barriers(
                        pending_barriers,
                        attempt,
                        fact,
                        execution,
                        lease_epoch,
                    )
                    execution.failed_resource_ids.discard(
                        resource.canonical_resource_id
                    )
                    return
            except StaleLeaseError:
                raise
            except Exception as exc:
                classification = self._classify_error(
                    exc, has_committed_segments=False, deadline=deadline
                )
                try:
                    observation = self._failed_resource_observation(
                        attempt,
                        resource,
                        parent_attempt_id,
                        definition,
                        classification,
                    )
                    self.repository.save_resource_observation(
                        observation,
                        owner_token=owner_token,
                        lease_epoch=lease_epoch,
                    )
                    observation_ids = (observation.observation_id,)
                except Exception:
                    observation_ids = ()
                fact = self._terminal(
                    attempt,
                    classification,
                    owner_token=owner_token,
                    resource_observation_ids=observation_ids,
                    work_position=position,
                )
                retry_delay = self._retry_delay(
                    classification, {}, definition, deadline
                )

            barrier = barrier_for_attempt(
                attempt=attempt,
                plan_item=fetch_plan,
                work_position=position,
                outcome=fact.outcome,
                reason_code=fact.reason_code,
                canonical_resource_id=resource.canonical_resource_id,
            )
            execution.barriers.append(barrier)
            pending_barriers.append(barrier)
            execution.reasons.append(fact.reason_code)
            execution.failed_resource_ids.add(resource.canonical_resource_id)
            if retry_delay is None or attempts_made >= int(
                definition.retry_policy.max_attempts
            ):
                return
            if self._monotonic() + retry_delay >= deadline:
                return
            if retry_delay:
                self._sleep(retry_delay)
            previous = attempt

    def _resolve_pending_fetch_barriers(
        self,
        barriers: Sequence[BarrierDraft],
        attempt: AcquisitionAttempt,
        fact: _AttemptFact,
        execution: _PlanExecution,
        lease_epoch: int,
    ) -> None:
        if not fact.snapshot_ids or not fact.resource_observation_ids:
            return
        for barrier in barriers:
            execution.barrier_resolutions.append(
                BarrierResolution(
                    barrier_id=barrier.barrier_id,
                    opening_attempt_id=barrier.opening_attempt_id,
                    resolving_attempt_id=attempt.attempt_id,
                    source_definition_id=barrier.source_definition_id,
                    source_definition_version=barrier.source_definition_version,
                    partition_key=barrier.partition_key,
                    work_position=barrier.work_position,
                    retry_group_id=barrier.retry_group_id,
                    canonical_resource_id=barrier.canonical_resource_id,
                    snapshot_id=fact.snapshot_ids[-1],
                    resource_observation_id=fact.resource_observation_ids[-1],
                    created_at=_utc(self._now()),
                    lease_epoch=lease_epoch,
                )
            )

    # Finalization ---------------------------------------------------------
    def _finalize(
        self,
        run: AcquisitionRun,
        executions: Mapping[str, _PlanExecution],
        coverage_entries: Sequence[CoverageEntry],
        coverage_links: Sequence[PhysicalQueryCoverageLink],
        *,
        lease_epoch: int,
        owner_token: str,
        heartbeat: _LeaseHeartbeat,
    ) -> AcquisitionExecutionResult:
        heartbeat.renew(force=True)
        links_by_coverage: dict[str, list[str]] = defaultdict(list)
        for link in coverage_links:
            links_by_coverage[link.coverage_entry_id].append(link.plan_item_id)
        resolutions: list[CoverageResolution] = []
        for entry in coverage_entries:
            if entry.plan_disposition == CoveragePlanDisposition.STATIC_POLICY_SKIPPED:
                resolutions.append(static_coverage_resolution(entry))
                continue
            related = [
                executions[plan_id]
                for plan_id in links_by_coverage.get(entry.coverage_entry_id, ())
                if plan_id in executions
            ]
            attempts = tuple(
                dict.fromkeys(
                    attempt_id
                    for item in related
                    for attempt_id in item.attempt_ids
                )
            )
            proofs = tuple(
                dict.fromkeys(proof for item in related for proof in item.proof_ids)
            )
            snapshots = tuple(
                dict.fromkeys(
                    snapshot
                    for item in related
                    for snapshot in (
                        item.discovery_snapshot_ids + item.content_snapshot_ids
                    )
                )
            )
            gaps = sum(item.material_gap_count for item in related) or (
                0 if related and all(item.complete for item in related) else 1
            )
            if gaps == 0 and related:
                status = CoverageResolutionStatus.COMPLETE
            elif any(item.proof_ids or item.content_snapshot_ids for item in related):
                status = CoverageResolutionStatus.PARTIAL
            else:
                status = CoverageResolutionStatus.BLOCKED
            reasons = tuple(
                dict.fromkeys(reason for item in related for reason in item.reasons)
            )
            resolutions.append(
                CoverageResolution(
                    coverage_entry_id=entry.coverage_entry_id,
                    status=status,
                    attempt_ids=attempts,
                    discovery_proof_ids=proofs,
                    snapshot_ids=snapshots,
                    material_gap_count=gaps,
                    reason_codes=reasons or (() if gaps == 0 else ("work_incomplete",)),
                    resolved_at=_utc(self._now()),
                    lease_epoch=lease_epoch,
                )
            )

        all_barriers = [item for value in executions.values() for item in value.barriers]
        barrier_resolutions = [
            item
            for value in executions.values()
            for item in value.barrier_resolutions
        ]
        resolved_ids = {item.barrier_id for item in barrier_resolutions}
        unresolved_barriers = [
            item for item in all_barriers if item.barrier_id not in resolved_ids
        ]
        checkpoint_builds = []
        if run.run_kind == AcquisitionRunKind.PRODUCTION:
            definitions = {
                (item.plan_item.source_definition_id, item.plan_item.source_definition_version)
                for item in executions.values()
            }
            for definition_id, definition_version in sorted(definitions):
                definition = self._source_definition_for_run(
                    run, definition_id, definition_version
                )
                previous = self.repository.latest_checkpoint(
                    run.ticker,
                    definition_id,
                    definition_version,
                    run.question_set_version,
                )
                progress = [
                    PlanProgress(
                        plan_item=value.plan_item,
                        complete=value.complete,
                        last_canonical_resource_id=(
                            max(value.resources) if value.resources else None
                        ),
                        validator_anchors=tuple(value.validator_anchors),
                    )
                    for value in executions.values()
                ]
                checkpoint_builds.append(
                    self.checkpoint_engine.build(
                        run=run,
                        source_definition_id=definition_id,
                        source_definition_version=definition_version,
                        overlap_days=definition.incremental_policy.overlap_days,
                        progress=progress,
                        opening_barriers=[
                            item
                            for item in all_barriers
                            if item.source_definition_id == definition_id
                            and item.source_definition_version == definition_version
                        ],
                        resolved_barrier_ids=resolved_ids,
                        previous=previous,
                        lease_epoch=lease_epoch,
                        created_at=_utc(self._now()),
                    )
                )

        coverage_accounted = len(resolutions) == len(coverage_entries)
        newly_unresolved_ids = {item.barrier_id for item in unresolved_barriers}
        carried_unresolved = []
        for build in checkpoint_builds:
            for barrier_id in build.checkpoint.unresolved_barrier_ids:
                if barrier_id in newly_unresolved_ids:
                    continue
                carried_unresolved.append(
                    self.repository.get_checkpoint_barrier(barrier_id)
                )
        gap_keys = {
            (
                barrier.source_definition_id,
                barrier.source_definition_version,
                barrier.partition_key,
                barrier.retry_group_id,
                barrier.work_position,
                barrier.canonical_resource_id,
            )
            for barrier in unresolved_barriers
        }
        gap_keys.update(
            (
                row["source_definition_id"],
                str(row["source_definition_version"]),
                row["partition_key"],
                row["retry_group_id"],
                row["work_position"],
                row.get("canonical_resource_id"),
            )
            for row in carried_unresolved
        )
        for execution in executions.values():
            if not execution.discovery_complete and not execution.barriers:
                gap_keys.add(
                    (
                        execution.plan_item.source_definition_id,
                        execution.plan_item.source_definition_version,
                        execution.plan_item.partition_key,
                        f"plan:{execution.plan_item.plan_item_id}",
                        "unaccounted",
                        None,
                    )
                )
        material_gap_count = len(gap_keys)
        candidate_eligible = bool(
            run.run_kind == AcquisitionRunKind.PRODUCTION
            and coverage_accounted
            and material_gap_count == 0
            and all(item.complete for item in executions.values())
            and all(
                not item.checkpoint.unresolved_barrier_ids
                for item in checkpoint_builds
            )
        )
        content_snapshot_ids = tuple(
            dict.fromkeys(
                snapshot
                for item in executions.values()
                for snapshot in item.content_snapshot_ids
            )
        )
        discovery_snapshot_ids = tuple(
            dict.fromkeys(
                snapshot
                for item in executions.values()
                for snapshot in item.discovery_snapshot_ids
            )
        )
        proof_ids = tuple(
            dict.fromkeys(
                proof for item in executions.values() for proof in item.proof_ids
            )
        )
        manifest_id: str | None = None
        default_consume_eligible = candidate_eligible
        if self.manifest_service is not None:
            heartbeat.renew(force=True)
            manifest = self.manifest_service.build_evidence_manifest(
                run_id=run.run_id,
                question_set_version=run.question_set_version,
                registry_id=run.registry_id,
                registry_version=run.registry_version,
                as_of=run.as_of,
                snapshot_ids=content_snapshot_ids,
                audit_reference_snapshot_ids=discovery_snapshot_ids,
                audit_proof_ids=proof_ids,
                coverage_summary={
                    "coverage_accounted": coverage_accounted,
                    "material_gap_count": material_gap_count,
                    "coverage_entry_count": len(coverage_entries),
                },
                fail_on_ineligible=False,
                default_consume_eligible=candidate_eligible,
            )
            manifest_id = manifest.manifest_id
            default_consume_eligible = bool(manifest.gate_passed)

        result = (
            AcquisitionRunResult.SUCCEEDED
            if coverage_accounted and material_gap_count == 0
            else AcquisitionRunResult.PARTIAL
        )
        final_event = AcquisitionRunEvent(
            run_id=run.run_id,
            event_type=AcquisitionRunEventType.FINALIZED,
            occurred_at=_utc(self._now()),
            lease_epoch=lease_epoch,
            result=result,
            coverage_accounted=coverage_accounted,
            material_gap_count=material_gap_count,
            default_consume_eligible=default_consume_eligible,
            reason_code=(None if material_gap_count == 0 else "material_gaps_present"),
            metadata={
                "manifest_id": manifest_id,
                "opening_barrier_count": len(all_barriers),
                "unresolved_barrier_count": len(gap_keys),
            },
        )
        checkpoints = [
            (item.checkpoint, item.expected_parent_version)
            for item in checkpoint_builds
        ]
        opening_barriers = [
            barrier
            for item in checkpoint_builds
            for barrier in item.opening_barriers
        ]
        # Manifest verification and checkpoint aggregation may be relatively
        # expensive.  Re-establish the lease immediately before the atomic
        # finalize transaction instead of relying only on the last timer tick.
        heartbeat.renew(force=True)
        self.repository.finalize_run(
            run.run_id,
            run_event=final_event,
            coverage_resolutions=resolutions,
            checkpoints=checkpoints,
            opening_barriers=opening_barriers,
            barrier_resolutions=barrier_resolutions,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )
        facts = self._attempt_facts(run.run_id)
        counts = Counter(item.outcome.value for item in facts)
        return AcquisitionExecutionResult(
            run_id=run.run_id,
            result=result,
            coverage_accounted=coverage_accounted,
            material_gap_count=material_gap_count,
            default_consume_eligible=default_consume_eligible,
            checkpoint_advanced=bool(checkpoints),
            lease_epoch=lease_epoch,
            outcome_counts=dict(sorted(counts.items())),
            attempt_ids=tuple(item.attempt.attempt_id for item in facts),
            coverage_resolution_ids=tuple(
                item.resolution_id for item in resolutions
            ),
            checkpoint_ids=tuple(
                item.checkpoint.checkpoint_id for item in checkpoint_builds
            ),
            manifest_id=manifest_id,
        )

    # Reconcile selection --------------------------------------------------
    def select_reconcile_target(
        self,
        parent_run_id: str,
        *,
        as_of: datetime | None = None,
    ) -> ReconcileSelection:
        parent = self.repository.get_run(parent_run_id)
        entries = self.repository.list_coverage_entries(parent_run_id)
        resolutions = self.repository.list_coverage_resolutions(parent_run_id)
        latest: dict[str, CoverageResolution] = {}
        for item in resolutions:
            latest[item.coverage_entry_id] = item
        unresolved = [
            item
            for item in entries
            if item.plan_disposition == CoveragePlanDisposition.REQUIRED
            and (
                item.coverage_entry_id not in latest
                or latest[item.coverage_entry_id].status
                in {
                    CoverageResolutionStatus.PARTIAL,
                    CoverageResolutionStatus.BLOCKED,
                }
            )
        ]
        parent_barriers = []
        for row in self.repository.list_checkpoint_barriers(unresolved_only=True):
            try:
                opening = self.repository.get_attempt(row["opening_attempt_id"])
            except Exception:
                continue
            if opening.run_id == parent_run_id:
                parent_barriers.append(row)
        quarantined_snapshot_ids: set[str] = set()
        resolution_by_coverage = {
            item.coverage_entry_id: item for item in resolutions
        }
        for resolution in resolutions:
            for snapshot_id in resolution.snapshot_ids:
                events = self.repository.list_snapshot_integrity_events(snapshot_id)
                if events and _enum(events[-1].status) == "quarantined":
                    quarantined_snapshot_ids.add(snapshot_id)
        integrity_entries = []
        for entry in entries:
            resolution = resolution_by_coverage.get(entry.coverage_entry_id)
            if resolution is not None and any(
                snapshot_id in quarantined_snapshot_ids
                for snapshot_id in resolution.snapshot_ids
            ):
                integrity_entries.append(entry)
        selected = unresolved or integrity_entries or [
            item
            for item in entries
            if item.plan_disposition == CoveragePlanDisposition.REQUIRED
        ]
        if not selected:
            raise AcquisitionExecutionError("parent run没有可reconcile的required范围")
        start_at = min(item.time_start for item in selected)
        current_registry_refs = {
            (item.source_definition_id, str(item.version))
            for item in self._definitions()
            if "business_model" in item.scopes
        }
        parent_refs = {
            (item.source_definition_id, str(item.version))
            for item in parent.source_definition_refs
        }
        registry_changed = current_registry_refs != parent_refs
        if unresolved or parent_barriers:
            strategy = "earliest_unresolved_gap"
        elif integrity_entries:
            strategy = "snapshot_integrity_failure"
        else:
            strategy = "earliest_completed_slice_periodic_history_recheck"
        target = {
            "parent_run_id": parent_run_id,
            "strategy": strategy,
            "start_at": start_at.isoformat(),
            "as_of": _utc(as_of or self._now()).isoformat(),
            "registry_compatibility_review_required": registry_changed,
            "unresolved_coverage_entry_ids": tuple(
                sorted(item.coverage_entry_id for item in unresolved)
            ),
            "unresolved_barrier_ids": tuple(
                sorted(row["barrier_id"] for row in parent_barriers)
            ),
            "quarantined_snapshot_ids": tuple(sorted(quarantined_snapshot_ids)),
        }
        return ReconcileSelection(
            parent_run_id=parent_run_id,
            start_at=start_at,
            target=target,
        )

    # Persistence helpers --------------------------------------------------
    def _start_attempt(
        self,
        run: AcquisitionRun,
        plan_item: PhysicalQueryPlanItem,
        *,
        attempt_kind: AttemptKind,
        lease_epoch: int,
        owner_token: str,
        work_position: str,
        retry_group_id: str,
        retry_ordinal: int,
        page: int | None = None,
        cursor: str | None = None,
        resource: DiscoveredResource | None = None,
        parent_discovery_attempt_id: str | None = None,
        supersedes: AcquisitionAttempt | None = None,
    ) -> AcquisitionAttempt:
        attempt = AcquisitionAttempt(
            run_id=run.run_id,
            source_definition_id=plan_item.source_definition_id,
            source_definition_version=plan_item.source_definition_version,
            physical_query_plan_item_id=plan_item.plan_item_id,
            execution_key=plan_item.execution_key,
            attempt_kind=attempt_kind,
            query_id=(plan_item.query_id if attempt_kind == AttemptKind.DISCOVERY else None),
            discovered_resource_id=(
                None if resource is None else resource.discovered_resource_id
            ),
            parent_discovery_attempt_id=parent_discovery_attempt_id,
            time_start=plan_item.time_start,
            time_end=plan_item.time_end,
            page_number=page,
            cursor=cursor,
            work_position=work_position,
            retry_group_id=retry_group_id,
            retry_ordinal=retry_ordinal,
            lease_epoch=lease_epoch,
            request_summary={
                "method": plan_item.request_method,
                "endpoint": plan_item.endpoint,
                "request_encoding": plan_item.request_encoding,
                "parameter_names": tuple(
                    sorted(
                        {
                            *plan_item.normalized_parameters,
                            *plan_item.parameter_binding_names,
                        }
                    )
                ),
                "fixed_header_names": tuple(sorted(plan_item.fixed_headers)),
            },
            started_at=_utc(self._now()),
            supersedes_attempt_id=(
                None if supersedes is None else supersedes.attempt_id
            ),
        )
        self.repository.save_attempt(attempt, owner_token=owner_token)
        self.repository.append_attempt_event(
            AcquisitionAttemptEvent(
                attempt_id=attempt.attempt_id,
                event_type=AcquisitionAttemptEventType.STARTED,
                occurred_at=attempt.started_at,
                lease_epoch=lease_epoch,
                protocol_summary={"work_position": work_position},
            ),
            owner_token=owner_token,
        )
        return attempt

    def _terminal(
        self,
        attempt: AcquisitionAttempt,
        classification: AttemptClassification,
        *,
        owner_token: str,
        proof_ids: Iterable[str] = (),
        snapshot_ids: Iterable[str] = (),
        resource_observation_ids: Iterable[str] = (),
        work_position: str | None = None,
    ) -> _AttemptFact:
        proof_tuple = tuple(dict.fromkeys(proof_ids))
        snapshot_tuple = tuple(dict.fromkeys(snapshot_ids))
        observation_tuple = tuple(dict.fromkeys(resource_observation_ids))
        self.repository.append_attempt_event(
            AcquisitionAttemptEvent(
                attempt_id=attempt.attempt_id,
                event_type=AcquisitionAttemptEventType.OUTCOME_TERMINAL,
                occurred_at=_utc(self._now()),
                lease_epoch=attempt.lease_epoch,
                outcome=classification.outcome,
                reason_code=classification.reason_code,
                proof_ids=proof_tuple,
                snapshot_ids=snapshot_tuple,
                protocol_summary={
                    "work_position": work_position or attempt.work_position,
                    "resource_observation_ids": observation_tuple,
                },
            ),
            owner_token=owner_token,
        )
        return _AttemptFact(
            attempt=attempt,
            outcome=classification.outcome,
            reason_code=classification.reason_code,
            proof_ids=proof_tuple,
            snapshot_ids=snapshot_tuple,
            resource_observation_ids=observation_tuple,
            work_position=work_position or attempt.work_position,
        )

    def _ensure_fetch_plan(
        self,
        parent: PhysicalQueryPlanItem,
        resource: DiscoveredResource,
        index: int,
    ) -> PhysicalQueryPlanItem:
        identity = {
            "run_id": parent.run_id,
            "parent_plan_item_id": parent.plan_item_id,
            "canonical_resource_id": resource.canonical_resource_id,
            "resource_url": resource.resource_url,
        }
        plan_id = stable_acquisition_id("fetch-plan", identity)
        try:
            return self.repository.get_physical_query_plan_item(plan_id)
        except Exception:
            pass
        plan = PhysicalQueryPlanItem(
            plan_item_id=plan_id,
            run_id=parent.run_id,
            source_definition_id=parent.source_definition_id,
            source_definition_version=parent.source_definition_version,
            query_id=parent.query_id,
            query_family=f"{parent.query_family}.required_fetch",
            execution_key=stable_acquisition_id("fetch-exec", identity),
            attempt_kind=AttemptKind.FETCH,
            request_method="GET",
            request_encoding="query",
            fixed_headers=parent.fixed_headers,
            endpoint=resource.resource_url,
            normalized_parameters={},
            partition_key=parent.partition_key,
            pagination_fingerprint=parent.pagination_fingerprint,
            ordinal=1_000_000 + int(hashlib.sha256(plan_id.encode()).hexdigest()[:8], 16),
            parent_plan_item_id=parent.plan_item_id,
            discovered_resource_id=resource.discovered_resource_id,
            time_start=parent.time_start,
            time_end=parent.time_end,
        )
        self.repository.save_physical_query_plan_item(plan)
        for link in self.repository.list_physical_query_coverage_links(
            plan_item_id=parent.plan_item_id
        ):
            self.repository.save_physical_query_coverage_link(
                PhysicalQueryCoverageLink(
                    plan_item_id=plan.plan_item_id,
                    coverage_entry_id=link.coverage_entry_id,
                )
            )
        return plan

    # Query, retry and classification helpers ------------------------------
    def _query_work(
        self,
        definition: SourceDefinition,
        query: SourceQueryDefinition,
        plan_item: PhysicalQueryPlanItem,
        *,
        run: AcquisitionRun,
        attempt: AcquisitionAttempt,
        heartbeat: _LeaseHeartbeat,
        page: int,
        cursor: str | None,
        deadline: float,
        url: str | None = None,
    ) -> QueryWork:
        parameters = dict(plan_item.normalized_parameters)
        pagination = query.pagination
        if plan_item.attempt_kind == AttemptKind.DISCOVERY:
            parameters.update(
                self._resolve_query_parameter_bindings(run, plan_item, query)
            )
            if pagination.strategy == "page" and pagination.page_parameter:
                parameters[pagination.page_parameter] = page
                if pagination.page_size_parameter:
                    parameters[pagination.page_size_parameter] = pagination.page_size
            elif (
                pagination.strategy == "cursor"
                and pagination.cursor_parameter
                and cursor
            ):
                parameters[pagination.cursor_parameter] = cursor
        fetch_policy = (
            "metadata_only"
            if run.run_kind == AcquisitionRunKind.SMOKE
            else query.fetch_policy.value
        )
        context = {
            "deadline_monotonic": deadline,
            "page_size": pagination.page_size,
            "fetch_policy": fetch_policy,
            "ticker": run.ticker,
            "items_path": query.pagination.items_path,
            "total_path": query.pagination.total_path,
        }
        encoding = plan_item.request_encoding
        return QueryWork(
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            query_id=query.query_id,
            query_family=query.query_family,
            execution_key=plan_item.execution_key,
            method=plan_item.request_method,
            url=url or plan_item.endpoint,
            page=page,
            cursor=cursor,
            params=(parameters if encoding == "query" else {}),
            json_body=(parameters if encoding == "json" else None),
            form_body=(parameters if encoding == "form" else None),
            headers=dict(plan_item.fixed_headers),
            expected_mime_types=tuple(query.discovery_schema.response_mime_types),
            max_response_bytes=definition.response_limits.max_response_bytes,
            parser_schema_version=query.discovery_schema.schema_version,
            context=context,
            execution_capability=self._transport_execution_capability(
                run,
                attempt,
                definition,
                heartbeat,
            ),
        )

    def _resolve_query_parameter_bindings(
        self,
        run: AcquisitionRun,
        plan_item: PhysicalQueryPlanItem,
        query: SourceQueryDefinition,
    ) -> dict[str, Any]:
        """Resolve frozen wire parameters only from earlier persisted proof rows."""

        if not query.parameter_bindings:
            return {}
        source_plans = [
            item
            for item in self.repository.list_physical_query_plan_items(run.run_id)
            if item.source_definition_id == plan_item.source_definition_id
            and str(item.source_definition_version)
            == str(plan_item.source_definition_version)
        ]
        resolved: dict[str, Any] = {}
        for parameter_name, binding in sorted(query.parameter_bindings.items()):
            expected = binding.match_value_template.replace("{ticker}", run.ticker)
            values: list[Any] = []
            for source_plan in source_plans:
                if source_plan.query_id != binding.source_query_id:
                    continue
                for source_attempt in self.repository.list_attempts(
                    plan_item_id=source_plan.plan_item_id
                ):
                    for observation in self.repository.list_discovery_observations(
                        source_attempt.attempt_id
                    ):
                        for resource in self.repository.list_discovered_resources(
                            observation.observation_id
                        ):
                            metadata = dict(resource.metadata)
                            if str(metadata.get(binding.match_metadata_key, "")) != expected:
                                continue
                            if binding.value_metadata_key not in metadata:
                                continue
                            values.append(metadata[binding.value_metadata_key])
            unique_values = list(dict.fromkeys(str(value) for value in values))
            if len(unique_values) != 1:
                raise DiscoveryValidationError(
                    "parameter_binding_missing",
                    "无法从已持久化discovery proof唯一解析请求参数"
                    f" {parameter_name} ({binding.source_query_id})",
                )
            resolved[parameter_name] = (
                binding.value_template.replace("{ticker}", run.ticker).replace(
                    "{value}", unique_values[0]
                )
            )
        return resolved

    def _transport_execution_capability(
        self,
        run: AcquisitionRun,
        attempt: AcquisitionAttempt,
        definition: SourceDefinition,
        heartbeat: _LeaseHeartbeat,
    ) -> TransportExecutionCapability:
        """Issue a request-scoped capability backed by durable lease state."""

        if (
            attempt.run_id != run.run_id
            or attempt.lease_epoch != heartbeat.lease_epoch
            or heartbeat.run_id != run.run_id
            or attempt.source_definition_id != definition.source_definition_id
            or str(attempt.source_definition_version) != str(definition.version)
        ):
            raise StaleLeaseError(
                "cannot issue transport capability for mismatched run/attempt/lease"
            )

        def guard(*, force: bool = False) -> None:
            # Renewal is the repository's atomic owner-token/epoch/expiry
            # fence.  Forced boundaries additionally re-read the immutable
            # run and attempt identities, so a capability cannot be reused for
            # another durable attempt or a different run ``as_of``.
            heartbeat.renew(force=force)
            if not force:
                return
            durable_attempt = self.repository.get_attempt(attempt.attempt_id)
            durable_run = self.repository.get_run(run.run_id)
            if (
                durable_attempt.attempt_id != attempt.attempt_id
                or durable_attempt.run_id != run.run_id
                or durable_attempt.lease_epoch != heartbeat.lease_epoch
                or durable_attempt.source_definition_id
                != definition.source_definition_id
                or str(durable_attempt.source_definition_version)
                != str(definition.version)
                or durable_run.run_id != run.run_id
                or _utc(durable_run.as_of) != _utc(run.as_of)
            ):
                raise StaleLeaseError(
                    "transport capability no longer matches durable execution context"
                )

        return TransportExecutionCapability(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            lease_epoch=attempt.lease_epoch,
            run_as_of=run.as_of,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            lease_guard=guard,
        )

    @staticmethod
    def _next_page(
        query: SourceQueryDefinition,
        proof: DiscoveryProof,
        page: int,
        cursor: str | None,
        *,
        adapter_next_cursor: str | None = None,
    ) -> tuple[int, str | None]:
        if proof.terminal:
            return page + 1, None
        if query.pagination.strategy == "none":
            raise DiscoveryValidationError(
                "terminal_proof_missing", "single-response query未证明终止"
            )
        if query.pagination.strategy == "page":
            if proof.declared_page_count is not None and page >= proof.declared_page_count:
                raise DiscoveryValidationError(
                    "server_page_limit_inconsistent",
                    "已到声明页数但响应未提供终止证明",
                )
            return page + 1, None
        # Cursor tokens are opaque source protocol values.  Fabricating a page
        # number can skip or duplicate upstream rows and cannot serve as audit
        # evidence of cursor progress.
        next_cursor = adapter_next_cursor
        if not next_cursor:
            raise DiscoveryValidationError(
                "next_cursor_missing", "cursor响应未提供下一游标"
            )
        if next_cursor == cursor:
            raise DiscoveryValidationError("duplicate_cursor", "cursor未推进")
        return page + 1, next_cursor

    def _retry_delay(
        self,
        classification: AttemptClassification,
        headers: Mapping[str, str],
        definition: SourceDefinition,
        deadline: float,
    ) -> float | None:
        if not classification.retryable:
            return None
        retry_policy = definition.retry_policy
        if classification.outcome == AcquisitionOutcome.RATE_LIMITED:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                delay = _parse_retry_after(retry_after, self._now())
                if delay is None or delay > retry_policy.retry_after_cap_seconds:
                    return None
                if self._monotonic() + delay >= deadline:
                    return None
                return delay
        return min(
            float(retry_policy.initial_backoff_seconds),
            float(retry_policy.max_backoff_seconds),
        )

    def _classify_error(
        self,
        exc: Exception,
        *,
        has_committed_segments: bool,
        deadline: float,
    ) -> AttemptClassification:
        if isinstance(exc, SecurityPolicyError):
            return classify_response(
                status_code=200,
                has_committed_segments=has_committed_segments,
                policy_reason=exc.reason_code,
            )
        if isinstance(exc, DiscoveryPipelineError):
            outcome = AcquisitionOutcome(exc.outcome)
            if has_committed_segments and outcome != AcquisitionOutcome.PARTIAL_SUCCESS:
                outcome = AcquisitionOutcome.PARTIAL_SUCCESS
            return AttemptClassification(outcome, exc.reason_code)
        if isinstance(exc, SnapshotPipelineError):
            return AttemptClassification(
                AcquisitionOutcome.PARSE_FAILED, exc.reason_code
            )
        return classify_exception(
            exc,
            has_committed_segments=has_committed_segments,
            deadline_was_exceeded=self._monotonic() >= deadline,
        )

    # Read-side helpers ----------------------------------------------------
    def _definitions(self) -> tuple[SourceDefinition, ...]:
        loaded = getattr(self.runtime, "loaded_registry", None)
        registry = getattr(loaded, "registry", loaded)
        return tuple(registry.definitions)

    def _frozen_definitions_for_run(
        self, run: AcquisitionRun
    ) -> tuple[SourceDefinition, ...]:
        resolver = getattr(self.runtime, "frozen_source_definitions", None)
        if resolver is None:
            raise AcquisitionExecutionError(
                "runtime不支持从repository恢复run固定的来源注册表"
            )
        try:
            return tuple(resolver(run))
        except Exception as exc:
            raise AcquisitionExecutionError(str(exc)) from exc

    def _source_definition_for_run(
        self,
        run: AcquisitionRun,
        definition_id: str,
        version: str,
    ) -> SourceDefinition:
        matches = [
            item
            for item in self._frozen_definitions_for_run(run)
            if item.source_definition_id == definition_id
            and str(item.version) == str(version)
        ]
        if len(matches) != 1:
            raise AcquisitionExecutionError(
                f"run固定的来源定义不可用: {definition_id}@{version}"
            )
        return matches[0]

    @staticmethod
    def _query_definition(
        definition: SourceDefinition, query_id: str
    ) -> SourceQueryDefinition:
        matches = [item for item in definition.queries if item.query_id == query_id]
        if len(matches) != 1:
            raise AcquisitionExecutionError(
                f"run固定的query不可用: {definition.source_definition_id}/{query_id}"
            )
        return matches[0]

    def _runtime_policy_reason(self, definition: SourceDefinition) -> str | None:
        if not definition.enabled:
            return "license_not_approved"
        automated = _enum(definition.license_policy.automated_access)
        if automated != "allowed":
            return "license_not_approved"
        if self._policy_guard is not None:
            try:
                if not self._policy_guard(definition):
                    return "runtime_policy_not_approved"
            except Exception:
                return "runtime_policy_not_approved"
        return None

    def _adapter_for(self, definition: SourceDefinition) -> Any:
        key = (definition.source_definition_id, str(definition.version))
        if key in self._adapter_cache:
            return self._adapter_cache[key]
        if self._transport_factory is not None:
            transport = self._transport_factory(definition)
        else:
            transport = RegistryBoundHttpTransport(
                definition,
                self.source_gate,
                client=getattr(self.runtime, "http_client", None),
                clock=self._monotonic,
                wall_clock=self._now,
            )
        if self._adapter_resolver is not None:
            adapter = self._adapter_resolver(definition, transport)
        else:
            adapter = self.adapter_factory.create(
                definition,
                transport=transport,
                snapshot_reader=self.runtime.snapshot_bytes,
            )
        self._transport_cache[key] = transport
        self._adapter_cache[key] = adapter
        return adapter

    def _close_transports(self) -> None:
        for transport in self._transport_cache.values():
            close = getattr(transport, "close", None)
            if close is not None:
                close()
        self._transport_cache.clear()
        self._adapter_cache.clear()

    def _latest_attempt(self, plan_item_id: str) -> AcquisitionAttempt | None:
        attempts = self.repository.list_attempts(plan_item_id=plan_item_id)
        return max(
            attempts,
            key=lambda item: (item.retry_ordinal, item.started_at, item.attempt_id),
            default=None,
        )

    def _prior_unresolved_barriers(
        self,
        plan_item: PhysicalQueryPlanItem,
        *,
        attempt_kind: AttemptKind,
        canonical_resource_id: str | None = None,
    ) -> tuple[tuple[BarrierDraft, ...], AcquisitionAttempt | None]:
        """Load compatible unresolved barriers from earlier finalized runs.

        A resolving attempt must preserve the immutable retry group/ordinal
        lineage.  We therefore select one compatible retry group at a time;
        unrelated sources, versions, query ranges, pagination semantics and
        resource positions remain open for a later targeted reconcile.
        """

        matches: list[tuple[dict[str, Any], AcquisitionAttempt]] = []
        current_run = self.repository.get_run(plan_item.run_id)
        for row in self.repository.list_checkpoint_barriers(
            source_definition_id=plan_item.source_definition_id,
            partition_key=plan_item.partition_key,
            unresolved_only=True,
        ):
            if str(row.get("source_definition_version")) != str(
                plan_item.source_definition_version
            ):
                continue
            if row.get("canonical_resource_id") != canonical_resource_id:
                continue
            try:
                opening = self.repository.get_attempt(row["opening_attempt_id"])
                opening_plan = self.repository.get_physical_query_plan_item(
                    opening.physical_query_plan_item_id
                )
                opening_run = self.repository.get_run(opening.run_id)
            except (KeyError, ValueError):
                continue
            if (
                opening_run.ticker != current_run.ticker
                or opening_run.question_set_version
                != current_run.question_set_version
            ):
                continue
            if opening.attempt_kind != attempt_kind:
                continue
            if any(
                (
                    opening_plan.query_id != plan_item.query_id,
                    opening_plan.partition_key != plan_item.partition_key,
                    opening_plan.time_start != plan_item.time_start,
                    opening_plan.time_end != plan_item.time_end,
                    opening_plan.pagination_fingerprint
                    != plan_item.pagination_fingerprint,
                )
            ):
                continue
            matches.append((row, opening))
        if not matches:
            return (), None
        selected_group = min(
            matches,
            key=lambda item: (str(item[0].get("created_at", "")), item[0]["barrier_id"]),
        )[1].retry_group_id
        selected = [item for item in matches if item[1].retry_group_id == selected_group]
        lineage = [
            attempt
            for attempt in self.repository.list_attempts()
            if attempt.retry_group_id == selected_group
        ]
        previous = max(
            lineage,
            key=lambda item: (item.retry_ordinal, item.started_at, item.attempt_id),
            default=max(
                (item[1] for item in selected),
                key=lambda item: item.retry_ordinal,
            ),
        )
        drafts = []
        for row, _opening in selected:
            created = row.get("created_at")
            if isinstance(created, str):
                created = datetime.fromisoformat(created)
            outcome = row.get("outcome")
            drafts.append(
                BarrierDraft(
                    barrier_id=row["barrier_id"],
                    source_definition_id=row["source_definition_id"],
                    source_definition_version=str(row["source_definition_version"]),
                    partition_key=row["partition_key"],
                    work_position=row["work_position"],
                    retry_group_id=row["retry_group_id"],
                    opening_attempt_id=row["opening_attempt_id"],
                    query_semantics_hash=row.get("query_semantics_hash"),
                    canonical_resource_id=row.get("canonical_resource_id"),
                    reason_code=row.get("reason_code"),
                    outcome=(None if outcome is None else AcquisitionOutcome(outcome)),
                    created_at=created or _utc(self._now()),
                )
            )
        return tuple(drafts), previous

    def _proof_for_barrier(
        self,
        attempt: AcquisitionAttempt,
        barrier: BarrierDraft,
    ) -> str | None:
        position = _parse_work_position(barrier.work_position)
        if position.get("kind") != "discovery":
            return None
        expected_page = position.get("page")
        expected_cursor = position.get("cursor")
        for proof in self.repository.list_discovery_proofs(attempt.attempt_id):
            if expected_page is not None and proof.page_number != expected_page:
                continue
            if proof.cursor != expected_cursor:
                continue
            return proof.proof_id
        return None

    def _resume_position(
        self,
        query: SourceQueryDefinition,
        attempt: AcquisitionAttempt | None,
    ) -> tuple[int, str | None]:
        if attempt is None:
            return 1, None
        segments = self.repository.list_attempt_segments(attempt.attempt_id)
        if segments:
            latest = max(segments, key=lambda item: item.segment_ordinal)
            parsed = _parse_work_position(latest.next_safe_position or "")
            if parsed:
                return int(parsed.get("page") or 1), parsed.get("cursor")
            if query.pagination.strategy == "page":
                return int(latest.page_number or 0) + 1, None
        parsed = _parse_work_position(attempt.work_position)
        return int(parsed.get("page") or attempt.page_number or 1), parsed.get(
            "cursor", attempt.cursor
        )

    def _load_existing_discovery(self, execution: _PlanExecution) -> None:
        attempts = self.repository.list_attempts(
            plan_item_id=execution.plan_item.plan_item_id
        )
        for attempt in attempts:
            if attempt.attempt_kind != AttemptKind.DISCOVERY:
                continue
            execution.discovery_attempt_ids.append(attempt.attempt_id)
            for proof in self.repository.list_discovery_proofs(attempt.attempt_id):
                if proof.proof_id not in execution.proof_ids:
                    execution.proof_ids.append(proof.proof_id)
                observation = self.repository.get_discovery_observation(
                    proof.observation_id
                )
                if observation.snapshot_id and observation.snapshot_id not in execution.discovery_snapshot_ids:
                    execution.discovery_snapshot_ids.append(observation.snapshot_id)
                for resource in self.repository.list_discovered_resources(
                    observation.observation_id
                ):
                    execution.resources.setdefault(
                        resource.canonical_resource_id, resource
                    )
            events = self.repository.list_attempt_events(attempt.attempt_id)
            for event in events:
                if event.event_type == AcquisitionAttemptEventType.OUTCOME_TERMINAL:
                    if event.outcome in {
                        AcquisitionOutcome.SUCCESS,
                        AcquisitionOutcome.NO_DATA,
                        AcquisitionOutcome.UNCHANGED,
                    }:
                        try:
                            self._proof_completion(execution.plan_item.plan_item_id)
                        except DiscoveryValidationError:
                            continue
                        execution.discovery_complete = True
                        execution.discovery_outcome = event.outcome

    def _proof_completion(self, plan_item_id: str) -> tuple[int, int | None]:
        attempts = self.repository.list_attempts(plan_item_id=plan_item_id)
        proofs: list[tuple[int, int, DiscoveryProof]] = []
        for attempt in attempts:
            for proof in self.repository.list_discovery_proofs(attempt.attempt_id):
                proofs.append((attempt.retry_ordinal, proof.page_number or 1, proof))
        if not proofs:
            raise DiscoveryValidationError("missing_page_proof", "缺少discovery proof")
        latest_by_page: dict[int, tuple[int, DiscoveryProof]] = {}
        for ordinal, page, proof in proofs:
            previous = latest_by_page.get(page)
            if previous is None or ordinal >= previous[0]:
                latest_by_page[page] = (ordinal, proof)
        ordered = [latest_by_page[key][1] for key in sorted(latest_by_page)]
        pages = [item.page_number or 1 for item in ordered]
        if pages != list(range(1, len(pages) + 1)):
            raise DiscoveryValidationError(
                "missing_page_proof", "discovery page proof不连续"
            )
        if any(not item.schema_valid for item in ordered):
            raise DiscoveryValidationError("schema_invalid", "discovery schema无效")
        terminals = [index for index, item in enumerate(ordered) if item.terminal]
        if terminals != [len(ordered) - 1]:
            raise DiscoveryValidationError(
                "terminal_proof_missing", "缺少唯一terminal discovery proof"
            )
        totals = {
            item.declared_total for item in ordered if item.declared_total is not None
        }
        if len(totals) > 1:
            raise DiscoveryValidationError(
                "total_mismatch", "discovery declared_total不一致"
            )
        normalized = sum(item.normalized_row_count for item in ordered)
        total = next(iter(totals)) if totals else None
        if total is not None and normalized != total:
            raise DiscoveryValidationError(
                "total_mismatch", "discovery总数与规范化资源不闭合"
            )
        if normalized == 0 and total != 0:
            raise DiscoveryValidationError(
                "total_missing_for_empty",
                "空结果缺少上游declared_total=0，不能判定no_data",
            )
        if not _declared_page_count_matches(ordered):
            raise DiscoveryValidationError(
                "page_count_mismatch", "discovery声明页数与proof不闭合"
            )
        return normalized, total

    def _validator_anchor(
        self,
        definition: SourceDefinition,
        resource: DiscoveredResource,
        *,
        force_unconditional: bool,
    ) -> tuple[ValidatorAnchor | None, dict[str, str]]:
        if force_unconditional:
            return None, {}
        snapshot = self.repository.find_raw_resource_snapshot(
            resource_role="content",
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            canonical_resource_id=resource.canonical_resource_id,
        )
        if snapshot is None or not self._snapshot_is_valid(snapshot):
            return None, {}
        observations = self.repository.list_resource_observations(
            snapshot_id=snapshot.snapshot_id
        )
        with_validator = [
            item for item in observations if item.etag or item.last_modified
        ]
        if not with_validator:
            return None, {}
        latest = max(
            with_validator,
            key=lambda item: (item.observed_at, item.observation_id),
        )
        anchor = ValidatorAnchor(
            canonical_resource_id=resource.canonical_resource_id,
            resource_url=resource.resource_url,
            snapshot_id=snapshot.snapshot_id,
            etag=latest.etag,
            last_modified=latest.last_modified,
            observed_at=latest.observed_at,
        )
        validators: dict[str, str] = {}
        if definition.incremental_policy.use_etag and anchor.etag:
            validators["etag"] = anchor.etag
        if definition.incremental_policy.use_last_modified and anchor.last_modified:
            validators["last_modified"] = anchor.last_modified
        return anchor, validators

    def _has_snapshot_candidate(
        self,
        definition: SourceDefinition,
        resource: DiscoveredResource,
    ) -> bool:
        """Whether a 304 refers to a known but unusable local anchor.

        This distinguishes an unsolicited 304 with no local history from a
        validator response tied to quarantined, missing-byte, or otherwise
        incompatible evidence.  Neither case may manufacture ``unchanged``.
        """

        return (
            self.repository.find_raw_resource_snapshot(
                resource_role="content",
                source_definition_id=definition.source_definition_id,
                source_definition_version=definition.version,
                canonical_resource_id=resource.canonical_resource_id,
            )
            is not None
        )

    def _snapshot_is_valid(self, snapshot: Any) -> bool:
        events = self.repository.list_snapshot_integrity_events(snapshot.snapshot_id)
        if events and _enum(events[-1].status) == "quarantined":
            return False
        try:
            self.runtime.snapshot_bytes(snapshot.snapshot_id)
        except Exception:
            return False
        return True

    def _record_failed_discovery_observation(
        self,
        attempt: AcquisitionAttempt,
        plan_item: PhysicalQueryPlanItem,
        definition: SourceDefinition,
        envelope: BoundedTransportEnvelope,
        *,
        owner_token: str,
        lease_epoch: int,
    ) -> None:
        save = getattr(self.repository, "save_discovery_observation", None)
        if save is None:
            return
        observation = self._discovery_failure_observation(
            attempt, plan_item, definition, envelope
        )
        save(
            observation,
            owner_token=owner_token,
            lease_epoch=lease_epoch,
        )

    @staticmethod
    def _discovery_failure_observation(
        attempt: AcquisitionAttempt,
        plan_item: PhysicalQueryPlanItem,
        definition: SourceDefinition,
        envelope: BoundedTransportEnvelope,
    ) -> Any:
        from .models import DiscoveryObservation

        return DiscoveryObservation(
            attempt_id=attempt.attempt_id,
            physical_query_plan_item_id=plan_item.plan_item_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            page_number=attempt.page_number,
            cursor=attempt.cursor,
            observed_at=envelope.observed_at,
            retrieved_at=envelope.retrieved_at,
            http_status=envelope.status_code,
            mime_type=envelope.content_type,
            response_sha256=envelope.sha256,
            response_byte_length=len(envelope.body),
            snapshot_id=None,
            request_summary={"url": envelope.request_url},
            response_summary={"headers": dict(envelope.headers)},
        )

    def _failed_resource_observation(
        self,
        attempt: AcquisitionAttempt,
        resource: DiscoveredResource,
        parent_attempt_id: str,
        definition: SourceDefinition,
        classification: AttemptClassification,
        *,
        envelope: BoundedTransportEnvelope | None = None,
    ) -> ResourceObservation:
        observed = _utc(
            envelope.observed_at if envelope is not None else self._now()
        )
        return ResourceObservation(
            attempt_id=attempt.attempt_id,
            discovered_resource_id=resource.discovered_resource_id,
            parent_discovery_attempt_id=parent_attempt_id,
            source_definition_id=definition.source_definition_id,
            source_definition_version=definition.version,
            snapshot_id=None,
            disposition=None,
            attempt_outcome=classification.outcome,
            original_url=resource.resource_url,
            final_url=(None if envelope is None else envelope.final_url),
            redirect_chain=(
                ()
                if envelope is None
                else tuple({"url": item} for item in envelope.redirect_chain)
            ),
            request_summary={},
            response_summary=(
                {} if envelope is None else {"headers": dict(envelope.headers)}
            ),
            http_status=None if envelope is None else envelope.status_code,
            etag=None if envelope is None else envelope.headers.get("etag"),
            last_modified=(
                None if envelope is None else envelope.headers.get("last-modified")
            ),
            observed_at=observed,
            retrieved_at=(None if envelope is None else envelope.retrieved_at),
            reason_code=classification.reason_code,
        )

    def _resource_completed_in_run(
        self, run_id: str, canonical_resource_id: str
    ) -> bool:
        for attempt in self.repository.list_attempts(run_id=run_id):
            if attempt.attempt_kind != AttemptKind.FETCH:
                continue
            try:
                resource = self.repository.list_resource_observations(
                    attempt_id=attempt.attempt_id
                )
            except Exception:
                continue
            if not resource:
                continue
            observation = resource[-1]
            try:
                discovered = self._discovered_resource(
                    observation.discovered_resource_id
                )
            except Exception:
                continue
            if discovered.canonical_resource_id != canonical_resource_id:
                continue
            events = self.repository.list_attempt_events(attempt.attempt_id)
            if any(
                event.outcome
                in {AcquisitionOutcome.SUCCESS, AcquisitionOutcome.UNCHANGED}
                for event in events
            ):
                return True
        return False

    def _load_resource_result(
        self,
        execution: _PlanExecution,
        run_id: str,
        resource: DiscoveredResource,
    ) -> None:
        for attempt in self.repository.list_attempts(run_id=run_id):
            if attempt.attempt_kind != AttemptKind.FETCH:
                continue
            observations = self.repository.list_resource_observations(
                attempt_id=attempt.attempt_id
            )
            for observation in observations:
                if observation.discovered_resource_id != resource.discovered_resource_id:
                    continue
                execution.fetch_attempt_ids.append(attempt.attempt_id)
                if observation.snapshot_id:
                    execution.content_snapshot_ids.append(observation.snapshot_id)
                    execution.resource_observation_ids.append(
                        observation.observation_id
                    )

    def _discovered_resource(self, discovered_resource_id: str) -> DiscoveredResource:
        # Repository intentionally exposes row-lineage lists rather than an
        # arbitrary URL lookup.  This bounded scan is only a compatibility path
        # used while reconstructing one already persisted run.
        for attempt in self.repository.list_attempts():
            if attempt.attempt_kind != AttemptKind.DISCOVERY:
                continue
            for observation in self.repository.list_discovery_observations(
                attempt.attempt_id
            ):
                for resource in self.repository.list_discovered_resources(
                    observation.observation_id
                ):
                    if resource.discovered_resource_id == discovered_resource_id:
                        return resource
        raise KeyError(discovered_resource_id)

    @staticmethod
    def _parent_discovery_attempt_id(execution: _PlanExecution) -> str:
        if not execution.discovery_attempt_ids:
            raise AcquisitionExecutionError("required fetch缺少父discovery attempt")
        return execution.discovery_attempt_ids[-1]

    def _attempt_facts(self, run_id: str) -> list[_AttemptFact]:
        facts: list[_AttemptFact] = []
        for attempt in self.repository.list_attempts(run_id=run_id):
            for event in self.repository.list_attempt_events(attempt.attempt_id):
                if event.event_type == AcquisitionAttemptEventType.OUTCOME_TERMINAL:
                    facts.append(
                        _AttemptFact(
                            attempt=attempt,
                            outcome=event.outcome,
                            reason_code=event.reason_code or "unspecified",
                            proof_ids=event.proof_ids,
                            snapshot_ids=event.snapshot_ids,
                        )
                    )
        return facts

    def _final_event(self, run_id: str) -> AcquisitionRunEvent | None:
        events = self.repository.list_run_events(run_id)
        finals = [
            item
            for item in events
            if item.event_type == AcquisitionRunEventType.FINALIZED
        ]
        return finals[-1] if finals else None

    def _result_from_final(
        self, run: AcquisitionRun, event: AcquisitionRunEvent
    ) -> AcquisitionExecutionResult:
        facts = self._attempt_facts(run.run_id)
        resolutions = self.repository.list_coverage_resolutions(run.run_id)
        checkpoints = []
        for reference in run.source_definition_refs:
            checkpoint = self.repository.latest_checkpoint(
                run.ticker,
                reference.source_definition_id,
                reference.version,
                run.question_set_version,
            )
            if checkpoint is not None and checkpoint.latest_successful_run_id == run.run_id:
                checkpoints.append(checkpoint.checkpoint_id)
        counts = Counter(item.outcome.value for item in facts)
        return AcquisitionExecutionResult(
            run_id=run.run_id,
            result=event.result,
            coverage_accounted=bool(event.coverage_accounted),
            material_gap_count=int(event.material_gap_count or 0),
            default_consume_eligible=bool(event.default_consume_eligible),
            checkpoint_advanced=bool(checkpoints),
            lease_epoch=int(event.lease_epoch or 1),
            outcome_counts=dict(sorted(counts.items())),
            attempt_ids=tuple(item.attempt.attempt_id for item in facts),
            coverage_resolution_ids=tuple(
                item.resolution_id for item in resolutions
            ),
            checkpoint_ids=tuple(checkpoints),
            manifest_id=event.metadata.get("manifest_id"),
        )


def _parse_retry_after(value: str, now: datetime) -> float | None:
    raw = value.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target.astimezone(timezone.utc) - _utc(now)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds)


__all__ = [
    "AcquisitionExecutionError",
    "AcquisitionExecutionResult",
    "AcquisitionNamespaceMismatch",
    "AcquisitionOrchestrator",
    "ReconcileSelection",
    "RunAlreadyFinalized",
]
