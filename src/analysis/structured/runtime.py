from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from analysis.acquisition.models import (
    AcquisitionAttempt,
    AcquisitionAttemptEvent,
    AcquisitionAttemptEventType,
    AcquisitionMode,
    AcquisitionOutcome,
    AcquisitionRun,
    AcquisitionRunEvent,
    AcquisitionRunEventType,
    AcquisitionRunKind,
    AcquisitionRunResult,
    AttemptKind,
    SourceDefinitionRef,
)
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.acquisition.repository import AcquisitionNotFoundError
from analysis.acquisition.snapshots import DiscoverySnapshotRequest

from .identity import SecurityIdentity
from .mappings import map_row
from .planner import (
    CompanyPlanTarget,
    DatasetWork,
    HistoryMode,
    PlanDisposition,
    StructuredDatasetPlanner,
    StructuredPlanningError,
)
from .pagination import PageAudit, PageManifest, PageSlice, PageStatus
from .protocols import (
    BaoStockQuery,
    EastmoneyRequest,
    ProtocolFamily,
    ResultStatus,
    baostock_anonymous_session,
    execute_baostock_query,
    parse_eastmoney_response,
    parse_em_f_company_type_response,
    plan_em_f,
)
from .records import RecordVersion
from .repair import (
    RepairManifest,
    plan_repair as build_repair_manifest,
    repair_candidates,
    repair_status as build_repair_status,
)
from .registry import StructuredRegistryBundle, StructuredRegistryLoader
from .scheduler import StructuredExecutionBridge
from .storage import (
    StructuredRunContext,
    StructuredStorage,
    canonical_json,
    canonical_sha256,
    stable_structured_id,
)


_PROTOCOLS = {
    "em_s": ProtocolFamily.EM_S,
    "em_w": ProtocolFamily.EM_W,
    "em_m": ProtocolFamily.EM_M,
    "em_f": ProtocolFamily.EM_F,
    "em_q": ProtocolFamily.EM_Q,
}
_SUPPLIER_QUERY_FLOORS = {
    "eastmoney": date(1990, 1, 1),
    "baostock": date(1990, 12, 19),
}
_MAX_HTTP_BYTES = 8 * 1024 * 1024


class StructuredRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class StructuredPlanResult:
    plan_id: str
    mode: str
    company_scope: str
    run_ids: tuple[str, ...]
    runs: tuple[Mapping[str, Any], ...]
    dataset_ids: tuple[str, ...]
    created: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "mode": self.mode,
            "company_scope": self.company_scope,
            "run_ids": list(self.run_ids),
            "runs": [dict(item) for item in self.runs],
            "dataset_ids": list(self.dataset_ids),
            "dataset_count": len(self.dataset_ids),
            "company_count": len(self.run_ids),
            "deferred_datasets_by_ticker": {
                item["ticker"]: item["acquisition_deferral"]
                for item in self.runs if item.get("acquisition_deferral")
            },
            "created": self.created,
            "persisted": True,
            "performed_network_io": False,
            "execution_boundary": "durable_plan_only",
        }


@dataclass(frozen=True)
class FrozenQuery:
    query_id: str
    query_family: str
    endpoint: str
    request_method: str
    request_encoding: str
    parameter_template: Mapping[str, Any]
    fixed_headers: Mapping[str, str]
    pagination: Mapping[str, Any]
    parameter_bindings: Mapping[str, Any]
    prerequisite_query_ids: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FrozenQuery":
        return cls(
            query_id=str(value["query_id"]),
            query_family=str(value["query_family"]),
            endpoint=str(value["endpoint"]),
            request_method=str(value["request_method"]),
            request_encoding=str(value["request_encoding"]),
            parameter_template=dict(value.get("parameter_template") or {}),
            fixed_headers=dict(value.get("fixed_headers") or {}),
            pagination=dict(value.get("pagination") or {}),
            parameter_bindings=dict(value.get("parameter_bindings") or {}),
            prerequisite_query_ids=tuple(value.get("prerequisite_query_ids") or ()),
        )


@dataclass(frozen=True)
class FrozenSource:
    source_definition_id: str
    version: str
    upstream_identity: str
    queries: tuple[FrozenQuery, ...]
    retry_policy: Any
    rate_limit: Any
    content_hash: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "source_definition_id": self.source_definition_id,
            "version": self.version,
            "upstream_identity": self.upstream_identity,
            "queries": [item.to_mapping() for item in self.queries],
            "retry_policy": {
                "max_attempts": int(self.retry_policy.max_attempts),
                "attempt_deadline_seconds": float(
                    self.retry_policy.attempt_deadline_seconds
                ),
            },
            "rate_limit": {
                "min_interval_seconds": float(self.rate_limit.min_interval_seconds)
            },
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FrozenSource":
        expected = canonical_sha256(
            {key: item for key, item in value.items() if key != "content_hash"}
        )
        if value.get("content_hash") != expected:
            raise StructuredRuntimeError("frozen structured source hash mismatch")
        retry = value.get("retry_policy") or {}
        rate = value.get("rate_limit") or {}
        return cls(
            source_definition_id=str(value["source_definition_id"]),
            version=str(value["version"]),
            upstream_identity=str(value["upstream_identity"]),
            queries=tuple(
                FrozenQuery.from_mapping(item) for item in value.get("queries") or ()
            ),
            retry_policy=SimpleNamespace(
                max_attempts=int(retry["max_attempts"]),
                attempt_deadline_seconds=float(retry["attempt_deadline_seconds"]),
            ),
            rate_limit=SimpleNamespace(
                min_interval_seconds=float(rate["min_interval_seconds"])
            ),
            content_hash=expected,
        )


@dataclass(frozen=True)
class ExecutionPage:
    status: ResultStatus
    rows: tuple[Mapping[str, Any], ...]
    page_number: int
    declared_total: int | None
    terminal: bool
    response_sha256: str
    body: bytes
    mime_type: str
    http_status: int | None
    diagnostic: str | None = None
    declared_pages: int | None = None


class StructuredDataRuntime:
    """Durable planner and explicitly invoked, bounded one-job executor."""

    def __init__(
        self,
        acquisition_runtime: AcquisitionRuntime,
        *,
        registry_bundle: StructuredRegistryBundle | None = None,
        sdk: Any | None = None,
    ) -> None:
        self.acquisition_runtime = acquisition_runtime
        self.bundle = registry_bundle or StructuredRegistryLoader().load()
        self.storage = StructuredStorage(
            acquisition_runtime.db_path,
            acquisition_runtime.namespace_id,
            data_root=acquisition_runtime.data_root,
            initialize=True,
        )
        self.repository = acquisition_runtime.repository
        self.planner = StructuredDatasetPlanner()
        self.bridge = StructuredExecutionBridge(
            repository=self.repository,
            storage=self.storage,
            source_gate=acquisition_runtime.source_gate,
            snapshot_service=acquisition_runtime.snapshot_service,
        )
        self.sdk = sdk
        self._sdk_session_active = False
        self._repair_active = False
        self._repair_query_io_count = 0
        self._sources = _build_sources(self.bundle)
        # Coverage entries and physical plan items are protected by the
        # acquisition control-plane foreign keys.  Persist the immutable
        # structured source versions before any plan can reference them.  This
        # is metadata-only and performs no upstream I/O.
        for source in self._sources.values():
            self.repository.save_source_definition_version(source.to_mapping())

    @classmethod
    def create(
        cls,
        db_path: Path | str,
        data_root: Path | str,
        *,
        registry_bundle: StructuredRegistryBundle | None = None,
        sdk: Any | None = None,
        **runtime_kwargs: Any,
    ) -> "StructuredDataRuntime":
        return cls(
            AcquisitionRuntime.create(db_path, data_root, **runtime_kwargs),
            registry_bundle=registry_bundle,
            sdk=sdk,
        )

    def plan(
        self,
        identities: Sequence[SecurityIdentity],
        *,
        mode: str,
        company_scope: str,
        dataset_ids: Sequence[str] | None,
        activated_on_demand_ids: Sequence[str] = (),
        report_periods: Sequence[str] = (),
        valuation_start: date | None = None,
        industry_profile_id: str | None = None,
        research_profile_id: str | None = None,
        parent_run_id: str | None = None,
        from_latest: bool = False,
        as_of: datetime,
    ) -> StructuredPlanResult:
        cutoff = _aware(as_of)
        selected_periods = tuple(sorted(set(report_periods)))
        if industry_profile_id or research_profile_id:
            from .scope import profile_fields, load_research_profile
        if industry_profile_id:
            profile_fields(industry_profile_id, "")
        if research_profile_id:
            load_research_profile(research_profile_id)
        if valuation_start and valuation_start > cutoff.date():
            raise ValueError("valuation_start_after_cutoff")
        if any(date.fromisoformat(p) > cutoff.date() for p in selected_periods):
            raise ValueError("report_period_after_cutoff")
        if not identities:
            raise ValueError("structured plan requires a resolved identity")
        if mode not in {"baseline", "incremental", "due", "reconcile"}:
            raise ValueError("unknown structured mode")
        if mode == "reconcile" and parent_run_id and from_latest:
            raise ValueError("reconcile不能同时指定parent_run_id和from_latest")
        if mode == "reconcile" and not parent_run_id and not from_latest:
            raise ValueError("reconcile必须指定parent_run_id或from_latest")
        if mode != "reconcile" and (parent_run_id or from_latest):
            raise ValueError("只有reconcile可以指定parent_run_id或from_latest")
        # Empty selects the versioned required research set, never all capabilities.
        from .scope import acquisition_deferral, selected_datasets, load_scope, scope_start
        selected_ids = selected_datasets(dataset_ids, research_profile_id)
        known_ids = {item.dataset_id for item in self.bundle.datasets.datasets}
        unknown = set(selected_ids or ()) - known_ids
        if unknown:
            raise ValueError(
                "unknown structured datasets: " + ", ".join(sorted(unknown))
            )
        requested_datasets = tuple(
            item
            for item in self.bundle.datasets.datasets
            if selected_ids is None or item.dataset_id in selected_ids
        )
        all_effective_ids: set[str] = set()
        summaries: list[dict[str, Any]] = []
        run_ids: list[str] = []
        created_flags: list[bool] = []
        for identity in identities:
            deferral = acquisition_deferral(
                identity.canonical_ticker, selected_ids,
                explicit_selection=bool(dataset_ids),
            )
            deferred_ids = set(deferral["dataset_ids"] if deferral else ())
            datasets = tuple(
                item for item in requested_datasets if item.dataset_id not in deferred_ids
            )
            if not datasets:
                raise StructuredPlanningError("all_selected_datasets_deferred")
            effective_ids = tuple(item.dataset_id for item in datasets)
            all_effective_ids.update(effective_ids)
            providers = {item.provider for item in datasets}
            sources = {
                key: item for key, item in self._sources.items()
                if item.upstream_identity in providers
            }
            selection_summary = {
                "dataset_ids": list(effective_ids),
                "acquisition_deferral": deferral,
            }
            reconcile_parent_id: str | None = None
            reconcile_target: dict[str, Any] | None = None
            if mode == "reconcile":
                reconcile_parent_id, reconcile_target = self._resolve_reconcile_target(
                    identity,
                    datasets,
                    parent_run_id=parent_run_id,
                    from_latest=from_latest,
                )
            run_id = stable_structured_id(
                "structured-run",
                {
                    "namespace": self.acquisition_runtime.namespace_id,
                    "company_id": identity.company_id,
                    "mode": mode,
                    "as_of": cutoff,
                    "dataset_ids": effective_ids,
                    "activated_on_demand_ids": tuple(activated_on_demand_ids),
                    "dataset_registry_hash": self.bundle.content_hashes["datasets"],
                    "field_registry_hash": self.bundle.content_hashes["fields"],
                    "research_scope_hash": load_scope()["content_sha256"],
                    **({"acquisition_deferral": deferral} if deferral else {}),
                    **({"research_profile_id": research_profile_id} if research_profile_id else {}),
                    "selected_report_periods": selected_periods,
                    **({"valuation_start": valuation_start.isoformat()} if valuation_start else {}),
                    **({"industry_profile_id": industry_profile_id} if industry_profile_id else {}),
                    **({"parent_run_id": reconcile_parent_id, "reconcile_target": reconcile_target}
                       if mode == "reconcile" else {}),
                },
            )
            complete_baseline = mode == "baseline" and not dataset_ids and not research_profile_id and not deferral
            run = AcquisitionRun(
                run_id=run_id,
                ticker=identity.security_code,
                company_name=identity.current_name,
                mode=(
                    AcquisitionMode.BASELINE
                    if mode == "baseline"
                    else AcquisitionMode.RECONCILE
                    if mode == "reconcile"
                    else AcquisitionMode.INCREMENTAL
                ),
                run_kind=(
                    AcquisitionRunKind.PRODUCTION
                    if complete_baseline
                    else AcquisitionRunKind.AD_HOC
                ),
                request_scope="complete" if complete_baseline else "ad_hoc",
                as_of=cutoff,
                created_at=cutoff,
                registry_id=self.acquisition_runtime.loaded_registry.registry.registry_id,
                registry_version=self.acquisition_runtime.loaded_registry.registry.registry_version,
                registry_content_hash=self.acquisition_runtime.loaded_registry.content_hash,
                question_set_id=(
                    self.acquisition_runtime.loaded_questions.question_set.question_set_id
                ),
                question_set_version=(
                    self.acquisition_runtime.loaded_questions.question_set.version
                ),
                question_set_content_hash=(
                    self.acquisition_runtime.loaded_questions.content_hash
                ),
                source_definition_refs=tuple(
                    SourceDefinitionRef(
                        source_definition_id=item.source_definition_id,
                        version=item.version,
                        content_hash=item.content_hash,
                    )
                    for _, item in sorted(sources.items())
                ),
                company_anchor_date=(
                    identity.listing_date or min(_SUPPLIER_QUERY_FLOORS.values())
                ),
                company_anchor_quality=(
                    "resolved_listing_date"
                    if identity.listing_date
                    else "conservative_supplier_query_floor"
                ),
                storage_namespace_id=self.acquisition_runtime.namespace_id,
                parent_run_id=reconcile_parent_id,
                reconcile_target=reconcile_target,
            )
            frozen = self._frozen_config(
                identity,
                datasets,
                sources,
                mode=mode,
                company_scope=company_scope,
                activated_on_demand_ids=activated_on_demand_ids,
                research_profile_id=research_profile_id,
            )
            if deferral:
                frozen["acquisition_deferral"] = deferral
            peer_set = self.bundle.peer_sets.peer_sets[0]
            frozen["selected_report_periods"] = list(selected_periods)
            if reconcile_target is not None:
                frozen["reconcile_target"] = reconcile_target
            if valuation_start:
                frozen["valuation_start"] = valuation_start.isoformat()
            if industry_profile_id:
                frozen['industry_profile_id']=industry_profile_id
                for dataset in frozen['datasets']:
                    dataset['research_profile_id']=industry_profile_id
            context = StructuredRunContext(
                run_id=run_id,
                storage_namespace_id=self.acquisition_runtime.namespace_id,
                ticker=identity.security_code,
                company_id=identity.company_id,
                dataset_registry_id=self.bundle.datasets.registry_id,
                dataset_registry_version=self.bundle.datasets.version,
                dataset_registry_hash=self.bundle.content_hashes["datasets"],
                field_registry_id=self.bundle.fields.registry_id,
                field_registry_version=self.bundle.fields.version,
                field_registry_hash=self.bundle.content_hashes["fields"],
                requirement_set_id=self.bundle.research_requirements.registry_id,
                requirement_set_version=self.bundle.research_requirements.version,
                requirement_set_hash=self.bundle.content_hashes[
                    "research_requirements"
                ],
                query_pack_id=f"{self.bundle.datasets.registry_id}.requests",
                query_pack_version=self.bundle.datasets.version,
                query_pack_hash=self.bundle.content_hashes["datasets"],
                source_registry_id=run.registry_id,
                source_registry_version=run.registry_version,
                source_registry_hash=run.registry_content_hash,
                peer_set_id=peer_set.peer_set_id,
                peer_set_version=self.bundle.peer_sets.version,
                peer_set_hash=self.bundle.content_hashes["peer_sets"],
                schedule_id=self.bundle.schedules.registry_id,
                schedule_version=self.bundle.schedules.version,
                schedule_hash=self.bundle.content_hashes["schedules"],
                policy_version="structured-data-first-v1",
                frozen_config=frozen,
                created_at=cutoff,
            )
            # Resolve an identical frozen request before reading the moving
            # incremental cursor. Replanning after completion otherwise changes
            # its windows and collides with a subset of its own initial jobs.
            try:
                existing_context = self.storage.get_run_context(run_id)
            except AcquisitionNotFoundError:
                existing_context = None
            if existing_context is not None:
                existing_run = self.repository.get_run(run_id)
                if (
                    existing_context.content_hash != context.content_hash
                    or existing_run.model_dump(mode="json") != run.model_dump(mode="json")
                ):
                    raise StructuredPlanningError(
                        "existing structured run has a different frozen request"
                    )
                run_ids.append(run_id)
                created_flags.append(False)
                summaries.append(
                    {
                        "run_id": run_id,
                        "company_id": identity.company_id,
                        "ticker": identity.canonical_ticker,
                        "job_count": len(self.storage.list_jobs(run_id, limit=None)),
                        "coverage_entry_count": len(self.repository.list_coverage_entries(run_id)),
                        "created": False,
                        **selection_summary,
                    }
                )
                continue
            incremental_starts: dict[str, date] = {}
            recovery_works: tuple[DatasetWork, ...] = ()
            if mode == "incremental":
                incremental_starts = self._incremental_starts(
                    identity,
                    datasets,
                    activated_on_demand_ids=activated_on_demand_ids,
                )
                recovery_works = self._incremental_recovery_works(identity, datasets)
            target = CompanyPlanTarget(
                company_id=identity.company_id,
                ticker=identity.security_code,
                listing_date=identity.listing_date,
                role="target",
                selection_reason="resolved_security_identity",
            )
            def earliest_available_at(item: Any) -> str:
                configured = (
                    (valuation_start or cutoff.date() - timedelta(days=14))
                    if item.dataset_id == "market_cap"
                    else min(date.fromisoformat(p) for p in selected_periods)
                    if selected_periods and "REPORT_DATE" in item.date_fields
                    else scope_start(cutoff.date(), research_profile_id)
                )
                if mode == "incremental" and item.dataset_id in incremental_starts:
                    configured = max(
                        configured,
                        incremental_starts[item.dataset_id],
                    )
                return configured.isoformat()

            planning_datasets = tuple(
                {
                    **item.model_dump(mode="json"),
                    # Bind the actual source contract; a local metadata fix
                    # need not invalidate the unchanged upstream protocol.
                    "source_definition_version": (
                        self.bundle.datasets.source_definition_version
                        or self.bundle.datasets.version
                    ),
                    "earliest_available_at": earliest_available_at(item),
                    "history_boundary_kind": (
                        "conservative_query_floor_not_availability_claim"
                    ),
                }
                for item in datasets
            )
            works = (
                self._reconcile_works(
                    identity,
                    planning_datasets,
                    reconcile_target or {},
                )
                if mode == "reconcile"
                else self.planner.plan_history(
                    companies=(target,),
                    datasets=planning_datasets,
                    as_of=cutoff,
                    selected_dataset_ids=effective_ids,
                    activated_dataset_ids=activated_on_demand_ids,
                )
            )
            if recovery_works:
                recovering = {work.dataset_id for work in recovery_works}
                works = tuple(work for work in works if work.dataset_id not in recovering) + recovery_works
                works = tuple(replace(work, ordinal=index) for index, work in enumerate(works))
            composed = self.planner.compose_shared_plan(
                run=run,
                context=context,
                works=works,
                source_definitions=sources,
            )
            persisted_run, created = self.planner.persist(
                self.storage,
                self.repository,
                context=context,
                plan=composed,
            )
            if persisted_run != run_id:
                raise StructuredPlanningError(
                    "plan overlaps a different frozen structured request"
                )
            run_ids.append(persisted_run)
            created_flags.append(created)
            summaries.append(
                {
                    "run_id": persisted_run,
                    "company_id": identity.company_id,
                    "ticker": identity.canonical_ticker,
                    "job_count": len(composed.jobs),
                    "coverage_entry_count": len(
                        composed.shared_plan.coverage_entries
                    ),
                    "created": created,
                    **selection_summary,
                }
            )
        return StructuredPlanResult(
            plan_id=stable_structured_id(
                "structured-plan",
                {
                    "run_ids": run_ids,
                    "mode": mode,
                    "company_scope": company_scope,
                },
            ),
            mode=mode,
            company_scope=company_scope,
            run_ids=tuple(run_ids),
            runs=tuple(summaries),
            dataset_ids=tuple(
                item.dataset_id for item in requested_datasets
                if item.dataset_id in all_effective_ids
            ),
            created=any(created_flags),
        )

    def _incremental_starts(
        self,
        identity: SecurityIdentity,
        datasets: Sequence[Any],
        *,
        activated_on_demand_ids: Sequence[str],
    ) -> dict[str, date]:
        """Resolve conservative per-dataset incremental floors.

        Structured coverage is the checkpoint for this runtime.  A proven
        empty query is complete; failed or partial partitions still require
        recovery so their unfinished windows cannot be skipped.
        """

        activated = set(activated_on_demand_ids)
        starts: dict[str, date] = {}
        missing: list[str] = []
        for item in datasets:
            dataset_id = str(item.dataset_id)
            history_mode = str(getattr(item, "history_mode", ""))
            if history_mode in {"on_demand", "on_demand_all_available_history"} and dataset_id not in activated:
                continue
            rows = self.storage.list_acquisition_coverage(
                company_id=identity.company_id,
                dataset_id=dataset_id,
                limit=None,
            )
            safe_rows = _effective_coverage(rows)
            if not safe_rows or any(
                not _query_complete(row) for row in safe_rows
            ):
                missing.append(dataset_id)
                continue
            try:
                safe_dates = [
                    datetime.fromisoformat(str(row["safe_through"]).replace("Z", "+00:00")).date()
                    for row in safe_rows
                ]
            except (TypeError, ValueError, KeyError):
                missing.append(dataset_id)
                continue
            floor_rows = []
            for row in safe_rows:
                try:
                    job = self.storage.get_job(str(row.get("job_id")))
                except Exception:
                    job = {}
                if job.get("purpose") != "report_period":
                    floor_rows.append(row)
            if not floor_rows:
                floor_rows = list(safe_rows)
            floor_dates = [
                datetime.fromisoformat(str(row["safe_through"]).replace("Z", "+00:00")).date()
                for row in floor_rows
            ]
            schedule = next(
                (
                    value
                    for value in self.bundle.schedules.dataset_schedules
                    if str(value.dataset_id) == dataset_id
                ),
                None,
            )
            overlap_days = int(getattr(schedule, "overlap_days", 0) or 0)
            # Event/lifecycle schedules deliberately re-read their bounded
            # overlap so late corrections cannot be lost.  Ordinary report
            # periods start at the safe upper bound and never re-fetch the
            # historical range.
            # The coverage rows include the original baseline partitions as
            # well as every later incremental window.  Use the latest safe
            # upper bound as the next cursor; taking the minimum would keep
            # every future run anchored to the oldest baseline window and
            # make the overlap grow without bound.  Failed/latest rows were
            # rejected above, so this cursor cannot jump past a known gap.
            starts[dataset_id] = max(
                max(floor_dates) - timedelta(days=overlap_days),
                _SUPPLIER_QUERY_FLOORS.get(str(getattr(item, "provider", "")), date(1990, 1, 1)),
            )
        return starts

    def _incremental_recovery_works(
        self, identity: SecurityIdentity, datasets: Sequence[Any],
    ) -> tuple[DatasetWork, ...]:
        """Recover unfinished windows independently of healthy datasets."""
        selected = {str(item.dataset_id) for item in datasets}
        jobs = self.storage.list_jobs(company_id=identity.company_id, limit=None)
        rows = self.storage.list_acquisition_coverage(company_id=identity.company_id, limit=None)
        effective = {row["scope_key"]: row for row in _effective_coverage(rows)}
        replaced_jobs = {job["reconciles_job_id"] for job in jobs if job.get("reconciles_job_id")}
        targets = []
        for job in jobs:
            if job["dataset_id"] not in selected or job["job_id"] in replaced_jobs:
                continue
            row = effective.get(job["scope_key"])
            if row is not None and _query_complete(row):
                continue
            targets.append({
                "job_id": job["job_id"], "dataset_id": job["dataset_id"],
                "scope_key": job["scope_key"], "status": (row or {}).get("status", "unfinished"),
                "reason_code": (row or {}).get("reason_code", "resume_unfinished_window"),
            })
        return self._reconcile_works(identity, datasets, {"items": targets}) if targets else ()

    def _resolve_reconcile_target(
        self,
        identity: SecurityIdentity,
        datasets: Sequence[Any],
        *,
        parent_run_id: str | None,
        from_latest: bool,
    ) -> tuple[str, dict[str, Any]]:
        """Resolve a finalized structured parent and its unsafe coverage only.

        Reconcile deliberately stays inside the structured control plane.  It
        never edits coverage or checkpoints; the derived run must execute and
        append a new coverage record before it can become the next cursor.
        """

        selected_ids = {str(item.dataset_id) for item in datasets}
        candidates: list[str] = []
        if parent_run_id:
            candidates = [str(parent_run_id)]
        elif from_latest:
            for run in self.repository.list_runs(
                ticker=identity.security_code, limit=100
            ):
                run_id = str(getattr(run, "run_id", ""))
                if not run_id:
                    continue
                try:
                    context = self.storage.get_run_context(run_id)
                except Exception:
                    continue
                if context.company_id == identity.company_id and any(
                    event.event_type == AcquisitionRunEventType.FINALIZED
                    for event in self.repository.list_run_events(run_id)
                ):
                    candidates.append(run_id)
                    break
        if not candidates:
            raise ValueError("reconcile找不到同公司已终结的structured parent run")
        resolved = candidates[0]
        try:
            parent = self.repository.get_run(resolved)
            context = self.storage.get_run_context(resolved)
        except Exception as exc:
            raise ValueError("reconcile parent不是可恢复的structured run") from exc
        if context.company_id != identity.company_id or parent.ticker != identity.security_code:
            raise ValueError("reconcile parent与当前公司身份不匹配")
        if not any(
            event.event_type == AcquisitionRunEventType.FINALIZED
            for event in self.repository.list_run_events(resolved)
        ):
            raise ValueError("reconcile parent尚未finalized")
        for name, expected in (
            ("dataset_registry_hash", self.bundle.content_hashes["datasets"]),
            ("field_registry_hash", self.bundle.content_hashes["fields"]),
            ("requirement_set_hash", self.bundle.content_hashes["research_requirements"]),
            ("schedule_hash", self.bundle.content_hashes["schedules"]),
        ):
            if getattr(context, name) != expected:
                raise ValueError(f"reconcile parent的{name}版本已变化")
        if context.source_registry_hash != self.acquisition_runtime.loaded_registry.content_hash:
            raise ValueError("reconcile parent的source registry版本已变化")

        rows = self.storage.list_acquisition_coverage(
            run_id=resolved, company_id=identity.company_id, limit=None
        )
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            dataset_id = str(row.get("dataset_id") or "")
            if dataset_id not in selected_ids:
                continue
            key = (dataset_id, str(row.get("scope_key") or ""))
            current = latest.get(key)
            marker = (int(row.get("version", 0) or 0), str(row.get("recorded_at") or ""))
            current_marker = (
                int(current.get("version", 0) or 0),
                str(current.get("recorded_at") or ""),
            ) if current else (-1, "")
            if current is None or marker > current_marker:
                latest[key] = row
        unsafe = [
            row for row in latest.values()
            if not _query_complete(row)
        ]
        # A transport/parser failure can terminate before a structured
        # coverage row is appended.  Treat that missing row as an explicit
        # unsafe target, while keeping successful jobs outside the target.
        covered_keys = set(latest)
        for job in self.storage.list_jobs(resolved, limit=None):
            dataset_id = str(job.get("dataset_id") or "")
            key = (dataset_id, str(job.get("scope_key") or ""))
            if dataset_id in selected_ids and key not in covered_keys:
                unsafe.append(
                    {
                        "coverage_record_id": None,
                        "job_id": job.get("job_id"),
                        "dataset_id": dataset_id,
                        "scope_key": key[1],
                        "status": "missing",
                        "reason_code": "coverage_missing_after_failed_job",
                    }
                )
        if not unsafe:
            raise ValueError("reconcile没有失败、空响应或不安全coverage目标")
        targets: list[dict[str, Any]] = []
        for row in sorted(unsafe, key=lambda value: (str(value.get("dataset_id")), str(value.get("scope_key")))):
            job_id = row.get("job_id")
            if not job_id:
                raise ValueError("reconcile coverage缺少原始job，无法安全恢复")
            try:
                job = self.storage.get_job(str(job_id))
            except Exception as exc:
                raise ValueError("reconcile coverage引用的job不存在") from exc
            targets.append(
                {
                    "coverage_record_id": str(
                        row.get("coverage_record_id") or f"missing:{job_id}"
                    ),
                    "job_id": str(job_id),
                    "dataset_id": str(row["dataset_id"]),
                    "scope_key": str(row["scope_key"]),
                    "status": str(row.get("status") or ""),
                    "reason_code": row.get("reason_code"),
                    "time_start": job.get("time_start"),
                    "time_end": job.get("time_end"),
                    "purpose": job.get("purpose"),
                }
            )
        return resolved, {
            "parent_run_id": resolved,
            "selection": "latest_unsafe_structured_coverage",
            "dataset_ids": sorted({item["dataset_id"] for item in targets}),
            "coverage_record_ids": [item["coverage_record_id"] for item in targets],
            "scope_keys": [item["scope_key"] for item in targets],
            "items": targets,
        }

    def _reconcile_works(
        self,
        identity: SecurityIdentity,
        datasets: Sequence[Any],
        target: Mapping[str, Any],
    ) -> tuple[DatasetWork, ...]:
        """Rebuild only the frozen query windows named by reconcile target."""

        dataset_ids = {
            str(item.get("dataset_id") if isinstance(item, Mapping) else item.dataset_id)
            for item in datasets
        }
        selected_items = list(target.get("items") or ())
        target_jobs = {item["job_id"] for item in selected_items}
        prerequisites = []
        for selected in selected_items:
            original = self.storage.get_job(str(selected["job_id"]))
            if original["purpose"] not in {"report_catalog", "report_period"}:
                continue
            for job in self.storage.list_jobs(original["run_id"], limit=None):
                if (job["dataset_id"] == original["dataset_id"] and job["purpose"] == "company_type"
                    and job["job_id"] not in target_jobs):
                    target_jobs.add(job["job_id"])
                    prerequisites.append({"job_id": job["job_id"], "dataset_id": job["dataset_id"],
                                          "scope_key": job["scope_key"], "status": "prerequisite"})
        selected_items = prerequisites + selected_items
        works: list[DatasetWork] = []
        for ordinal, selected in enumerate(selected_items):
            dataset_id = str(selected.get("dataset_id") or "")
            if dataset_id not in dataset_ids:
                continue
            try:
                job = self.storage.get_job(str(selected["job_id"]))
                plan_item = self.repository.get_physical_query_plan_item(job["plan_item_id"])
            except Exception as exc:
                raise ValueError("reconcile目标的原始job或plan不存在") from exc
            start = _parse_datetime(job.get("time_start"))
            end = _parse_datetime(job.get("time_end"))
            if start is None or end is None or end <= start:
                raise ValueError("reconcile原始job缺少有效时间范围")
            try:
                history_mode = HistoryMode(str(job["schedule_mode"]))
            except (KeyError, ValueError) as exc:
                raise ValueError("reconcile原始job的schedule_mode无法恢复") from exc
            works.append(
                DatasetWork(
                    company_id=identity.company_id,
                    ticker=identity.security_code,
                    dataset_id=dataset_id,
                    source_definition_id=str(plan_item.source_definition_id),
                    source_definition_version=str(plan_item.source_definition_version),
                    query_id=str(plan_item.query_id),
                    purpose=str(job["purpose"]),
                    history_mode=history_mode,
                    disposition=PlanDisposition.REQUIRED,
                    time_start=start,
                    time_end=end,
                    partition_key=str(plan_item.partition_key or selected["scope_key"]),
                    parameters=dict(plan_item.normalized_parameters or {}),
                    ordinal=ordinal,
                    reconciles_job_id=str(job["job_id"]),
                    reason_code=(
                        f"reconcile:{selected.get('status')}:"
                        f"{selected.get('reason_code') or 'unsafe_coverage'}"
                    ),
                )
            )
        if not works:
            raise ValueError("reconcile目标不包含当前研究范围内的可恢复job")
        return tuple(works)

    def execute(self, run_id: str, *, max_jobs_per_round: int = 1) -> dict[str, Any]:
        if max_jobs_per_round < 1:
            raise ValueError("max_jobs_per_round must be positive")
        context = self.bridge.prepare_execution(run_id)
        candidates = self.bridge.resume_candidates(run_id)[:max_jobs_per_round]
        if not candidates:
            self._finalize_if_terminal(run_id, self.bridge.status(run_id))
            return {
                "run_id": run_id,
                "single_round": True,
                "attempted_job_ids": [],
                "frozen_context_hash": context.content_hash,
                "status": self.status(run_id),
            }
        lease, owner_token = self.bridge.claim_lease(
            run_id,
            now=self.acquisition_runtime.clock(),
            ttl_seconds=120,
        )
        attempted: list[str] = []
        try:
            self.repository.append_run_event(
                AcquisitionRunEvent(
                    event_id=stable_structured_id(
                        "structured-running",
                        {"run_id": run_id, "lease_epoch": lease.lease_epoch},
                    ),
                    run_id=run_id,
                    event_type=AcquisitionRunEventType.RUNNING,
                    lease_epoch=lease.lease_epoch,
                    occurred_at=self.acquisition_runtime.clock(),
                    metadata={"single_round": True},
                ),
                owner_token=owner_token,
            )
            for candidate in candidates:
                attempted.append(candidate.job_id)
                self._execute_job(
                    context,
                    candidate,
                    owner_token=owner_token,
                    lease_epoch=lease.lease_epoch,
                )
        finally:
            self.repository.release_lease(
                run_id,
                owner_token=owner_token,
                lease_epoch=lease.lease_epoch,
                now=self.acquisition_runtime.clock(),
            )
        status = self.bridge.status(run_id)
        self._finalize_if_terminal(run_id, status)
        return {
            "run_id": run_id,
            "single_round": True,
            "attempted_job_ids": attempted,
            "frozen_context_hash": context.content_hash,
            "status": self.status(run_id),
        }

    def resume(self, run_id: str) -> dict[str, Any]:
        return self.execute(run_id, max_jobs_per_round=1)

    def status(self, run_id: str) -> dict[str, Any]:
        context = self.bridge.prepare_execution(run_id)
        status = self.bridge.status(run_id)
        jobs = {job["job_id"]: job for job in self.storage.list_jobs(run_id, limit=None)}
        datasets = {item["dataset_id"]: item for item in context.frozen_config["datasets"]}
        coverage = _effective_coverage(
            self.storage.list_acquisition_coverage(run_id=run_id, limit=None)
        )
        coverage_by_job = {row["job_id"]: row for row in coverage}
        unfinished = []
        for item in status.jobs:
            if item.state in {"succeeded", "no_data"}:
                continue
            job = jobs[item.job_id]
            next_page, finish_saved = None, False
            if not item.blocked_by:
                try:
                    next_page, _, terminal, _ = self._resume_checkpoint(context, job, datasets[item.dataset_id])
                    finish_saved = terminal is not None
                except (ValueError, OSError, StructuredRuntimeError):
                    pass
            unfinished.append({
                "job_id": item.job_id, "dataset_id": item.dataset_id, "state": item.state,
                "reason": (coverage_by_job.get(item.job_id) or {}).get("reason_code"),
                "missing_prerequisite_job_ids": list(item.blocked_by),
                "time_start": job.get("time_start"), "time_end": job.get("time_end"),
                "resume_page": next_page,
                "resume_action": "wait_for_prerequisite" if item.blocked_by else
                    "finish_saved_terminal" if finish_saved else
                    "incremental_or_reconcile" if item.state == "failed" else "resume",
            })
        return {
            **_status_mapping(status),
            "summary": {
                "updated": [item.job_id for item in status.jobs if item.state == "succeeded"],
                "empty": [item.job_id for item in status.jobs if item.state == "no_data"],
                "unfinished": unfinished,
                "runnable_finished": not any(item.state in {"pending", "retryable", "partial"} for item in status.jobs),
            },
            "completed_empty_jobs": sum(
                row.get("status") == "no_data" and _query_complete(row)
                for row in coverage
            ),
            "frozen_context_hash": context.content_hash,
            "dataset_ids": [item["dataset_id"] for item in context.frozen_config["datasets"]],
            "acquisition_deferral": context.frozen_config.get("acquisition_deferral"),
            "frozen_versions": {
                "datasets": context.dataset_registry_version,
                "fields": context.field_registry_version,
                "requirements": context.requirement_set_version,
                "schedule": context.schedule_version,
                "policy": context.policy_version,
            },
        }

    def repair_plan(
        self,
        run_id: str,
        *,
        dataset_filters: Sequence[str],
        reason_filters: Sequence[str],
        code_revision: str,
    ) -> RepairManifest:
        return build_repair_manifest(
            bridge=self.bridge,
            storage=self.storage,
            repository=self.repository,
            run_id=run_id,
            dataset_filters=dataset_filters,
            reason_filters=reason_filters,
            code_revision=code_revision,
        )

    def repair_status(
        self,
        manifest: RepairManifest,
        *,
        expected_code_revision: str | None = None,
    ) -> dict[str, Any]:
        return build_repair_status(
            manifest,
            bridge=self.bridge,
            storage=self.storage,
            repository=self.repository,
            expected_code_revision=expected_code_revision,
        )

    def repair_execute(
        self,
        manifest: RepairManifest,
        *,
        expected_code_revision: str,
        max_jobs_per_round: int = 25,
    ) -> dict[str, Any]:
        """Execute one explicit, bounded repair round from a verified manifest."""

        if max_jobs_per_round < 1:
            raise ValueError("max_jobs_per_round must be positive")
        context = self.bridge.prepare_execution(manifest.run_id)
        candidates = repair_candidates(
            manifest,
            bridge=self.bridge,
            storage=self.storage,
            repository=self.repository,
            expected_code_revision=expected_code_revision,
        )[:max_jobs_per_round]
        if not candidates:
            return {
                **self.repair_status(
                    manifest, expected_code_revision=expected_code_revision
                ),
                "single_round": True,
                "attempted_job_ids": [],
            }

        frozen = _validate_frozen_config(context.frozen_config)
        datasets = {
            str(item["dataset_id"]): item for item in frozen["datasets"]
        }
        providers = {
            str(datasets[candidate.dataset_id]["provider"])
            for candidate in candidates
            if candidate.dataset_id in datasets
        }
        if len(providers) != 1 or not providers.issubset({"baostock", "eastmoney"}):
            raise StructuredRuntimeError(
                "repair round must contain one frozen supported provider"
            )
        source_keys = {
            (
                str(job["source_definition_id"]),
                str(job["source_definition_version"]),
            )
            for candidate in candidates
            for job in (self.storage.get_job(candidate.job_id),)
        }
        sources = {
            (item.source_definition_id, item.version): item
            for item in (
                FrozenSource.from_mapping(value)
                for value in frozen["source_definitions"]
            )
        }
        if len(source_keys) != 1 or next(iter(source_keys)) not in sources:
            raise StructuredRuntimeError(
                "repair round must contain one frozen source version"
            )
        source = sources[next(iter(source_keys))]

        lease, owner_token = self.bridge.claim_lease(
            manifest.run_id,
            now=self.acquisition_runtime.clock(),
            ttl_seconds=120,
        )
        attempted: list[str] = []
        source_status = "available"
        source_diagnostic: str | None = None
        source_probe_performed = False
        self._repair_active = True
        self._repair_query_io_count = 0
        try:
            with ExitStack() as stack:
                if providers == {"baostock"}:
                    if self.sdk is None:
                        source_status = "source_unavailable"
                        source_diagnostic = "StructuredRuntimeError:BaoStock SDK is not bound"
                    else:
                        guard = self._lease_guard(
                            manifest.run_id,
                            owner_token=owner_token,
                            lease_epoch=lease.lease_epoch,
                        )
                        with self.bridge.hold_source(
                            run_id=manifest.run_id,
                            source_definition=source,
                            host="baostock-sdk.local",
                            deadline_monotonic=(
                                self.acquisition_runtime.monotonic_clock()
                                + float(
                                    source.retry_policy.attempt_deadline_seconds
                                )
                            ),
                            lease_guard=guard,
                        ):
                            guard(force=True)
                            source_probe_performed = True
                            try:
                                stack.enter_context(
                                    baostock_anonymous_session(self.sdk)
                                )
                            except Exception as exc:
                                source_status = "source_unavailable"
                                source_diagnostic = _safe_diagnostic(exc)
                            else:
                                guard(force=True)
                                self._sdk_session_active = True
                if source_status == "available":
                    for candidate in candidates:
                        attempted.append(candidate.job_id)
                        self._execute_job(
                            context,
                            candidate,
                            owner_token=owner_token,
                            lease_epoch=lease.lease_epoch,
                        )
        finally:
            self._sdk_session_active = False
            self._repair_active = False
            self.repository.release_lease(
                manifest.run_id,
                owner_token=owner_token,
                lease_epoch=lease.lease_epoch,
                now=self.acquisition_runtime.clock(),
            )
        status = self.repair_status(
            manifest, expected_code_revision=expected_code_revision
        )
        return {
            **status,
            "single_round": True,
            "source_status": source_status,
            "source_diagnostic": source_diagnostic,
            "fallback_required": source_status != "available",
            "attempted_job_ids": attempted,
            "source_probe_performed": source_probe_performed,
            "query_io_count": self._repair_query_io_count,
            "performed_network_io": (
                source_probe_performed or self._repair_query_io_count > 0
            ),
        }

    def _frozen_config(
        self,
        identity: SecurityIdentity,
        datasets: Sequence[Any],
        sources: Mapping[tuple[str, str], FrozenSource],
        *,
        mode: str,
        company_scope: str,
        activated_on_demand_ids: Sequence[str],
        research_profile_id: str | None = None,
    ) -> dict[str, Any]:
        dataset_ids = {item.dataset_id for item in datasets}
        frozen = {
            "schema": "structured-execution.v1",
            "research_scope": __import__("analysis.structured.scope", fromlist=["load_scope"]).load_scope(),
            "identity": _jsonable(asdict(identity)),
            "mode": mode,
            "company_scope": company_scope,
            "activated_on_demand_ids": list(activated_on_demand_ids),
            "datasets": [item.model_dump(mode="json") | {"research_scope_id": "eight-step-scope-v1.0.0"} for item in datasets],
            "known_fields": {
                dataset_id: [
                    item.raw_name
                    for item in self.bundle.fields.fields
                    if item.dataset_id == dataset_id
                ]
                for dataset_id in dataset_ids
            },
            "source_definitions": [
                item.to_mapping()
                for _, item in sorted(sources.items(), key=lambda pair: pair[0])
            ],
            "supplier_query_floors": {
                key: value.isoformat() for key, value in _SUPPLIER_QUERY_FLOORS.items()
            },
            "history_boundary_kind": (
                "conservative_query_floor_not_availability_claim"
            ),
        }
        if research_profile_id:
            from .scope import load_research_profile
            frozen["research_profile_id"] = research_profile_id
            frozen["research_profile"] = load_research_profile(research_profile_id)
            for dataset in frozen["datasets"]:
                dataset["research_scope_profile_id"] = research_profile_id
        return frozen

    def _resume_checkpoint(
        self, context: StructuredRunContext, job: Mapping[str, Any], dataset: Mapping[str, Any],
    ) -> tuple[int, list[dict[str, Any]], Any, bool]:
        """Verify the saved prefix; replay a valid terminal or retry a bad page."""
        pages = self.storage.list_pages(job["job_id"])
        if not pages:
            return 1, [], None, False
        plan = self.repository.get_physical_query_plan_item(job["plan_item_id"])
        records = self.storage.list_records(job_id=job["job_id"], limit=None)
        manifest = PageManifest(dataset_id=str(dataset["dataset_id"]))
        paginated = dataset["history_enumeration"] in {"complete_pagination", "on_demand_complete_pagination"}
        prefix: list[dict[str, Any]] = []
        for expected, saved in enumerate(pages, start=1):
            if int(saved.get("page_number") or 0) != expected:
                return expected, prefix, None, True
            params = _bind_parameters(
                dataset, context.frozen_config["identity"], job, page_number=expected,
                plan_parameters=plan.normalized_parameters,
                selected_report_periods=context.frozen_config.get("selected_report_periods", ()),
                resolved_company_type=(None if job["purpose"] == "company_type"
                                       else self._load_em_f_company_type(context, dataset)),
            )
            try:
                page, observed_at = self._replay_snapshot(
                    job, dataset, params, snapshot_id=saved["snapshot_id"], page_number=expected,
                )
            except (ValueError, OSError, StructuredRuntimeError):
                return expected, prefix, None, True
            keys = tuple(str(record["row_key"]) for record in records if record["page_id"] == saved["page_id"])
            if (page.status not in {ResultStatus.SUCCESS, ResultStatus.EMPTY}
                or len(keys) != len(page.rows) or saved.get("row_count") != len(page.rows)):
                return expected, prefix, None, True
            if paginated:
                manifest.add(PageSlice(
                    page_number=expected, row_keys=keys, response_sha256=page.response_sha256,
                    status=PageStatus.EMPTY if page.status is ResultStatus.EMPTY else PageStatus.SUCCESS,
                    declared_total=page.declared_total, declared_pages=page.declared_pages,
                    terminal=page.terminal,
                ))
                audit = manifest.audit()
                defects = tuple(issue for issue in audit.issues if issue not in {
                    "terminal_page_missing", "declared_page_count_mismatch", "declared_record_count_mismatch",
                })
                if defects or (page.terminal and not audit.complete):
                    if any(issue in audit.issues for issue in ("declared_total_drift", "declared_pages_drift")):
                        return 1, [], None, True
                    return expected, prefix, None, True
            if page.terminal:
                return expected, prefix, (page, observed_at, saved), False
            prefix.append(saved)
        return len(prefix) + 1, prefix, None, False

    def _execute_job(
        self,
        context: StructuredRunContext,
        candidate: Any,
        *,
        owner_token: str,
        lease_epoch: int,
    ) -> None:
        job = self.storage.get_job(candidate.job_id)
        plan_item = self.repository.get_physical_query_plan_item(job["plan_item_id"])
        frozen = _validate_frozen_config(context.frozen_config)
        dataset = next(item for item in frozen["datasets"] if item["dataset_id"] == job["dataset_id"])
        page_number, pages, saved_terminal, retry_invalid = self._resume_checkpoint(context, job, dataset)
        inherited_pages: dict[int, dict[str, Any]] = {}
        original_job = None
        if job.get("reconciles_job_id"):
            original_job = self.storage.get_job(job["reconciles_job_id"])
            original_context = self.storage.get_run_context(original_job["run_id"])
            _, prefix, original_terminal, _ = self._resume_checkpoint(original_context, original_job, dataset)
            if original_terminal:
                prefix = prefix + [original_terminal[2]]
            inherited_pages = {int(page["page_number"]): page for page in prefix}
        retry_ordinal = int(candidate.next_retry_ordinal or 0)
        attempt_id = stable_structured_id(
            "structured-attempt",
            {
                "job_id": candidate.job_id,
                "retry_ordinal": retry_ordinal,
                "page_number": page_number,
            },
        )
        attempt = AcquisitionAttempt(
            attempt_id=attempt_id,
            run_id=context.run_id,
            source_definition_id=job["source_definition_id"],
            source_definition_version=job["source_definition_version"],
            physical_query_plan_item_id=job["plan_item_id"],
            execution_key=plan_item.execution_key,
            attempt_kind=AttemptKind.DISCOVERY,
            query_id=plan_item.query_id,
            time_start=_parse_datetime(job.get("time_start")),
            time_end=_parse_datetime(job.get("time_end")),
            page_number=page_number,
            work_position=f"page:{page_number}",
            retry_group_id=stable_structured_id(
                "structured-retry", {"job_id": candidate.job_id}
            ),
            retry_ordinal=retry_ordinal,
            lease_epoch=lease_epoch,
            request_summary={
                "dataset_id": job["dataset_id"],
                "page_number": page_number,
            },
            started_at=self.acquisition_runtime.clock(),
        )
        self.bridge.save_attempt(attempt, owner_token=owner_token)
        self.repository.append_attempt_event(
            AcquisitionAttemptEvent(
                event_id=stable_structured_id(
                    "structured-attempt-started", {"attempt_id": attempt_id}
                ),
                attempt_id=attempt_id,
                event_type=AcquisitionAttemptEventType.STARTED,
                lease_epoch=lease_epoch,
                occurred_at=self.acquisition_runtime.clock(),
            ),
            owner_token=owner_token,
        )
        terminal_written = False
        snapshot_ids: list[str] = []
        validated_page_ids = [str(item["page_id"]) for item in pages]
        guard = self._lease_guard(context.run_id, owner_token=owner_token, lease_epoch=lease_epoch)
        try:
            frozen = _validate_frozen_config(context.frozen_config)
            dataset = next(
                (
                    item
                    for item in frozen["datasets"]
                    if item["dataset_id"] == job["dataset_id"]
                ),
                None,
            )
            if dataset is None:
                raise StructuredRuntimeError(
                    "job dataset is absent from frozen structured context"
                )
            sources = {
                (item.source_definition_id, item.version): item
                for item in (
                    FrozenSource.from_mapping(value)
                    for value in frozen["source_definitions"]
                )
            }
            source = sources.get(
                (job["source_definition_id"], job["source_definition_version"])
            )
            if source is None:
                raise StructuredRuntimeError(
                    "job source is absent from frozen structured context"
                )
            pagination_manifest: PageManifest | None = None
            if dataset["history_enumeration"] in {
                "complete_pagination",
                "on_demand_complete_pagination",
            }:
                pagination_manifest = PageManifest(
                    dataset_id=str(dataset["dataset_id"]),
                    partition={"job_id": str(job["job_id"])},
                )
                existing_records = self.storage.list_records(
                    job_id=job["job_id"], limit=None
                )
                records_by_page: dict[str, list[str]] = {}
                for record in existing_records:
                    records_by_page.setdefault(str(record["page_id"]), []).append(
                        str(record["row_key"])
                    )
                for existing_page in pages:
                    page_number_value = int(existing_page.get("page_number") or 0)
                    if page_number_value < 1:
                        continue
                    status_value = str(existing_page.get("status") or "success")
                    page_status = (
                        PageStatus.EMPTY
                        if status_value == ResultStatus.EMPTY.value
                        else PageStatus.FAILED
                        if status_value in {
                            ResultStatus.FAILED.value,
                            ResultStatus.PARTIAL.value,
                        }
                        else PageStatus.SUCCESS
                    )
                    content_hash = existing_page.get("content_hash")
                    if not content_hash:
                        continue
                    pagination_manifest.add(
                        PageSlice(
                            page_number=page_number_value,
                            row_keys=tuple(
                                records_by_page.get(str(existing_page.get("page_id")), ())
                            ),
                            response_sha256=str(content_hash),
                            status=page_status,
                            declared_total=(
                                None
                                if existing_page.get("total_count") is None
                                else int(existing_page["total_count"])
                            ),
                            declared_pages=(
                                None
                                if existing_page.get("declared_pages") is None
                                else int(existing_page["declared_pages"])
                            ),
                            terminal=bool(existing_page.get("terminal")),
                        )
                    )
            guard = self._lease_guard(
                context.run_id,
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )
            while True:
                if page_number > 10_000:
                    raise StructuredRuntimeError(
                        "structured pagination exceeded the bounded page limit"
                    )
                params = _bind_parameters(
                    dataset,
                    frozen["identity"],
                    job,
                    page_number=page_number,
                    plan_parameters=plan_item.normalized_parameters,
                    selected_report_periods=frozen.get('selected_report_periods',()),
                    resolved_company_type=(
                        None
                        if job["purpose"] == "company_type"
                        else self._load_em_f_company_type(context, dataset)
                    ),
                )
                reusable_id = None if retry_invalid else self._find_reusable_snapshot(
                    job,
                    dataset,
                    params,
                    page_number=page_number,
                )
                if saved_terminal is not None:
                    execution_page, observed_at, saved_page = saved_terminal
                    snapshot_id = str(saved_page["snapshot_id"])
                    saved_terminal = None
                elif reusable_id is None:
                    inherited = inherited_pages.get(page_number)
                    if inherited is not None and original_job is not None:
                        execution_page, _ = self._replay_snapshot(
                            original_job, dataset, params,
                            snapshot_id=inherited["snapshot_id"], page_number=page_number,
                        )
                    elif dataset["provider"] == "eastmoney":
                        execution_page = self._execute_http(
                            context.run_id,
                            dataset,
                            source,
                            params,
                            purpose=job["purpose"],
                            page_number=page_number,
                            lease_guard=guard,
                        )
                    else:
                        execution_page = self._execute_sdk(
                            context.run_id,
                            dataset,
                            source,
                            params,
                            page_number=page_number,
                            lease_guard=guard,
                        )
                    observed_at = self.acquisition_runtime.clock()
                    frozen_snapshot = (
                        self.acquisition_runtime.snapshot_service.freeze_discovery_response(
                            execution_page.body,
                            DiscoverySnapshotRequest(
                                attempt_id=attempt_id,
                                physical_query_plan_item_id=job["plan_item_id"],
                                source_definition_id=job["source_definition_id"],
                                source_definition_version=job["source_definition_version"],
                                query_page_canonical=canonical_json(
                                    {
                                        "dataset_id": job["dataset_id"],
                                        "params": params,
                                        "page_number": page_number,
                                    }
                                ),
                                mime_type=execution_page.mime_type,
                                observed_at=observed_at,
                                retrieved_at=observed_at,
                                page_number=page_number,
                                policy_decision="allowed",
                                http_status=execution_page.http_status,
                                request_summary={
                                    "dataset_id": job["dataset_id"],
                                    "page_number": page_number,
                                    **({"reused_from_job_id": original_job["job_id"]}
                                       if inherited is not None and original_job is not None else {}),
                                },
                                response_summary={
                                    "status": execution_page.status.value,
                                    "row_count": len(execution_page.rows),
                                    "diagnostic": execution_page.diagnostic,
                                },
                                observation_id=stable_structured_id(
                                    "structured-observation",
                                    {
                                        "attempt_id": attempt_id,
                                        "page_number": page_number,
                                    },
                                ),
                            ),
                            owner_token=owner_token,
                            lease_epoch=lease_epoch,
                        )
                    )
                    snapshot_id = frozen_snapshot.snapshot.snapshot_id
                else:
                    snapshot_id = reusable_id
                    execution_page, observed_at = self._replay_snapshot(
                        job,
                        dataset,
                        params,
                        snapshot_id=snapshot_id,
                        page_number=page_number,
                    )
                snapshot_ids.append(snapshot_id)
                if execution_page.status is ResultStatus.FAILED:
                    self._append_acquisition_coverage(
                        context, job, execution_page, snapshot_id, observed_at
                    )
                    self._append_attempt_outcome(
                        attempt_id,
                        lease_epoch,
                        AcquisitionOutcome.PARSE_FAILED,
                        owner_token,
                        reason_code=execution_page.diagnostic or "protocol_failure",
                        snapshot_ids=tuple(snapshot_ids),
                    )
                    terminal_written = True
                    return
                records, fields = self._project_rows(
                    context,
                    job,
                    dataset,
                    execution_page,
                    snapshot_id,
                    observed_at,
                )
                absent_periods: tuple[str, ...] = ()
                if job["purpose"] == "report_period":
                    requested = {
                        str(value).strip()
                        for value in str(
                            plan_item.normalized_parameters.get(
                                "report_period",
                                plan_item.normalized_parameters.get("dates", ""),
                            )
                        ).split(",")
                        if str(value).strip()
                    }
                    observed = {
                        str(row.get("REPORT_DATE")).strip()[:10]
                        for row in execution_page.rows
                        if row.get("REPORT_DATE") not in (None, "")
                    }
                    absent_periods = tuple(sorted(requested - observed))
                pagination_audit: PageAudit | None = None
                if pagination_manifest is not None:
                    page_status = (
                        PageStatus.EMPTY
                        if execution_page.status is ResultStatus.EMPTY
                        else PageStatus.FAILED
                        if execution_page.status in {
                            ResultStatus.FAILED,
                            ResultStatus.PARTIAL,
                        }
                        else PageStatus.SUCCESS
                    )
                    pagination_manifest.add(
                        PageSlice(
                            page_number=page_number,
                            row_keys=tuple(str(record["row_key"]) for record in records),
                            response_sha256=execution_page.response_sha256,
                            status=page_status,
                            declared_total=execution_page.declared_total,
                            declared_pages=execution_page.declared_pages,
                            terminal=execution_page.terminal,
                        )
                    )
                    if execution_page.terminal:
                        pagination_audit = pagination_manifest.audit()
                page_payload = {
                    "page_id": _page_id(job["job_id"], execution_page),
                    "job_id": job["job_id"],
                    "attempt_id": attempt_id,
                    "snapshot_id": snapshot_id,
                    "page_number": page_number,
                    "position_key": f"page:{page_number}",
                    "row_count": len(records),
                    "total_count": execution_page.declared_total,
                    "declared_pages": execution_page.declared_pages,
                    "terminal": execution_page.terminal,
                    "content_hash": execution_page.response_sha256,
                    "lease_epoch": lease_epoch,
                    "status": execution_page.status.value,
                    "committed_at": observed_at,
                }
                if absent_periods:
                    page_payload["absent_periods"] = list(absent_periods)
                if pagination_audit is not None and not pagination_audit.complete:
                    page_payload["pagination_issues"] = list(pagination_audit.issues)
                if not any(item["page_id"] == page_payload["page_id"]
                           for item in self.storage.list_pages(job["job_id"], include_history=True)):
                    self.bridge.commit_page_bundle(
                        page=page_payload, records=records, fields=fields,
                        owner_token=owner_token, lease_epoch=lease_epoch,
                    )
                validated_page_ids.append(page_payload["page_id"])
                guard(force=True)
                self._append_acquisition_coverage(
                    context,
                    job,
                    execution_page,
                    snapshot_id,
                    observed_at,
                    pagination_audit=pagination_audit,
                    absent_periods=absent_periods,
                    validated_page_ids=validated_page_ids,
                    completion_attempt_id=attempt_id,
                )
                if (
                    job["purpose"] == "report_catalog"
                    and execution_page.status is ResultStatus.SUCCESS
                    and execution_page.terminal
                ):
                    guard(force=True)
                    self._expand_em_f_report_batches(
                        context,
                        job,
                        dataset,
                        execution_page.rows,
                    )
                    guard(force=True)
                if pagination_audit is not None and not pagination_audit.complete:
                    reason = "pagination_incomplete:" + ",".join(
                        pagination_audit.issues
                    )
                    self._append_attempt_outcome(
                        attempt_id,
                        lease_epoch,
                        AcquisitionOutcome.PARTIAL_SUCCESS,
                        owner_token,
                        reason_code=reason,
                        snapshot_ids=tuple(snapshot_ids),
                    )
                    terminal_written = True
                    return
                if (
                    execution_page.status is ResultStatus.PARTIAL
                    or execution_page.terminal
                ):
                    outcome = {
                        ResultStatus.SUCCESS: AcquisitionOutcome.SUCCESS,
                        ResultStatus.EMPTY: AcquisitionOutcome.NO_DATA,
                        ResultStatus.PARTIAL: AcquisitionOutcome.PARTIAL_SUCCESS,
                    }[execution_page.status]
                    reason_code = execution_page.diagnostic
                    if absent_periods:
                        absent_reason = "supplier_period_absent:" + ",".join(
                            absent_periods
                        )
                        reason_code = (
                            f"{reason_code};{absent_reason}"
                            if reason_code
                            else absent_reason
                        )
                    self._append_attempt_outcome(
                        attempt_id,
                        lease_epoch,
                        outcome,
                        owner_token,
                        reason_code=reason_code,
                        snapshot_ids=tuple(snapshot_ids),
                        protocol_summary=(
                            {"absent_periods": list(absent_periods)}
                            if absent_periods
                            else None
                        ),
                    )
                    terminal_written = True
                    return
                page_number += 1
        except Exception as exc:
            if terminal_written:
                return
            outcome = (
                AcquisitionOutcome.TIMEOUT
                if isinstance(exc, httpx.TimeoutException)
                else AcquisitionOutcome.NETWORK_FAILED
                if isinstance(exc, httpx.RequestError)
                else AcquisitionOutcome.PARSE_FAILED
            )
            guard(force=True)
            self._append_acquisition_coverage(
                context, job, None, snapshot_ids[-1] if snapshot_ids else None,
                self.acquisition_runtime.clock(),
                failure_reason=f"{type(exc).__name__}:{str(exc)[:200]}",
            )
            self._append_attempt_outcome(
                attempt_id,
                lease_epoch,
                outcome,
                owner_token,
                reason_code=f"{type(exc).__name__}:{str(exc)[:200]}",
                snapshot_ids=tuple(snapshot_ids),
            )

    def _load_em_f_company_type(
        self,
        context: StructuredRunContext,
        dataset: Mapping[str, Any],
    ) -> str | None:
        if dataset["request"]["protocol"] != "em_f":
            return None
        values: set[str] = set()
        for item in self.storage.list_jobs(context.run_id, limit=None):
            if item["purpose"] != "company_type":
                continue
            for record in self.storage.list_records(
                job_id=item["job_id"], limit=None
            ):
                value = (record.get("raw_row") or {}).get("companyType")
                if value not in (None, ""):
                    values.add(str(value))
        if not values:
            raise StructuredRuntimeError(
                "EM-F companyType prerequisite has not completed"
            )
        if len(values) != 1:
            raise StructuredRuntimeError(
                "EM-F companyType prerequisite is conflicting"
            )
        return next(iter(values))

    def _expand_em_f_report_batches(
        self,
        context: StructuredRunContext,
        job: Mapping[str, Any],
        dataset: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Persist report work derived only from the committed catalog rows."""

        statement = _em_f_statement(dataset)
        dates = tuple(
            sorted(
                {
                    date.fromisoformat(str(row["REPORT_DATE"])[:10])
                    for row in rows
                    if row.get("REPORT_DATE") not in (None, "")
                },
                reverse=True,
            )
        )
        if context.frozen_config.get("research_scope"):
            from .scope import scope_start
            cutoff = self.repository.get_run(context.run_id).as_of.date()
            chosen = context.frozen_config.get("selected_report_periods")
            floor = min([scope_start(cutoff, context.frozen_config.get("research_profile_id"))]+[date.fromisoformat(p) for p in chosen or []])
            dates = tuple(d for d in dates if floor <= d <= cutoff)
            if chosen:
                dates = tuple(d for d in dates if d.isoformat() in chosen)
            elif context.frozen_config.get('mode') in {'incremental','due'}:
                known=self.storage.committed_report_periods(context.company_id,job['dataset_id'],context.run_id)
                recent=set(sorted(dates,reverse=True)[:2])
                dates=tuple(d for d in dates if d in recent or d.isoformat() not in known)
        if not dates:
            raise StructuredRuntimeError(
                "successful EM-F report catalog contains no report dates"
            )
        company_type = self._load_em_f_company_type(context, dataset)
        identity = context.frozen_config["identity"]
        plan = plan_em_f(
            company_code=_provider_code(dataset, identity),
            company_type=company_type,
            catalogs={statement: dates},
            dataset_ids=(job["dataset_id"],),
            batch_size=5,
        )
        existing = self.storage.list_jobs(context.run_id, limit=None)
        ordinal = max((int(item["ordinal"]) for item in existing), default=-1) + 1
        works: list[DatasetWork] = []
        for offset, batch in enumerate(plan.report_batches):
            first = min(batch.dates)
            last = max(batch.dates)
            works.append(
                DatasetWork(
                    company_id=context.company_id,
                    ticker=context.ticker,
                    dataset_id=batch.dataset_id,
                    source_definition_id=job["source_definition_id"],
                    source_definition_version=job["source_definition_version"],
                    query_id=batch.dataset_id,
                    purpose="report_period",
                    history_mode=HistoryMode.REPORT_CATALOG,
                    disposition=PlanDisposition.REQUIRED,
                    time_start=datetime.combine(first, datetime.min.time(), tzinfo=timezone.utc),
                    time_end=datetime.combine(
                        last + timedelta(days=1),
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    ),
                    partition_key=(
                        f"{context.company_id}:{batch.dataset_id}:reports:"
                        + ",".join(item.isoformat() for item in batch.dates)
                    ),
                    parameters=dict(batch.params),
                    ordinal=ordinal + offset,
                )
            )
        if not works:
            return
        frozen_sources = {
            (item.source_definition_id, item.version): item
            for item in (
                FrozenSource.from_mapping(value)
                for value in context.frozen_config["source_definitions"]
            )
        }
        extension = self.planner.compose_shared_plan(
            run=self.repository.get_run(context.run_id),
            context=context,
            works=tuple(works),
            source_definitions=frozen_sources,
        )
        self.storage.persist_plan_bundle(
            self.repository,
            run=extension.shared_plan.run,
            context=context,
            plan_items=extension.shared_plan.physical_query_plan_items,
            coverage_entries=extension.shared_plan.coverage_entries,
            links=extension.shared_plan.coverage_links,
            jobs=extension.jobs,
        )

    def _find_reusable_snapshot(
        self,
        job: Mapping[str, Any],
        dataset: Mapping[str, Any],
        params: Mapping[str, Any],
        *,
        page_number: int,
    ) -> str | None:
        expected_query = canonical_json(
            {
                "dataset_id": dataset["dataset_id"],
                "params": params,
                "page_number": page_number,
            }
        )
        for snapshot_id in self.storage.unprojected_snapshot_ids(job["job_id"]):
            snapshot = self.repository.get_raw_resource_snapshot(snapshot_id)
            if snapshot.query_page_canonical != expected_query:
                continue
            observation = self.repository.get_discovery_observation(
                self.storage.snapshot_observation_id(job["job_id"], snapshot_id)
            )
            if str(observation.response_summary.get("status")) in {
                ResultStatus.SUCCESS.value,
                ResultStatus.EMPTY.value,
            }:
                return snapshot_id
        return None

    def _replay_snapshot(
        self,
        job: Mapping[str, Any],
        dataset: Mapping[str, Any],
        params: Mapping[str, Any],
        *,
        snapshot_id: str,
        page_number: int,
    ) -> tuple[ExecutionPage, datetime]:
        """Rebuild a page from an immutable saved response without network I/O."""

        snapshot = self.repository.get_raw_resource_snapshot(snapshot_id)
        expected_query = canonical_json(
            {
                "dataset_id": dataset["dataset_id"],
                "params": params,
                "page_number": page_number,
            }
        )
        if snapshot.query_page_canonical != expected_query:
            raise StructuredRuntimeError(
                "unprojected snapshot does not match the frozen query page"
            )
        observation = self.repository.get_discovery_observation(
            self.storage.snapshot_observation_id(job["job_id"], snapshot_id)
        )
        body = self.acquisition_runtime.blob_store.read_verified(
            snapshot.archive_relative_path,
            expected_sha256=snapshot.sha256,
            expected_length=snapshot.byte_length,
        )
        if dataset["provider"] == "eastmoney":
            protocol = _PROTOCOLS[dataset["request"]["protocol"]]
            endpoint = _execution_endpoint(dataset, purpose=job["purpose"])
            request_params = dict(params)
            if protocol is ProtocolFamily.EM_F and endpoint.endswith("DateAjaxNew"):
                request_params.pop("dates", None)
                request_params.pop("reportType", None)
            if observation.http_status is None:
                raise StructuredRuntimeError(
                    "saved Eastmoney response is missing its HTTP status"
                )
            request = EastmoneyRequest(
                protocol=protocol,
                endpoint=endpoint,
                params=request_params,
                max_response_bytes=_MAX_HTTP_BYTES,
            )
            if job["purpose"] == "company_type":
                evidence = parse_em_f_company_type_response(
                    request,
                    status_code=observation.http_status,
                    body=body,
                    content_type=observation.mime_type,
                )
                rows = (
                    ({"code": evidence.company_code, "companyType": evidence.company_type},)
                    if evidence.company_type
                    else ()
                )
                execution_page = ExecutionPage(
                    status=evidence.proof.status,
                    rows=rows,
                    page_number=page_number,
                    declared_total=len(rows),
                    terminal=True,
                    declared_pages=1,
                    response_sha256=evidence.proof.response_sha256,
                    body=body,
                    mime_type=observation.mime_type or "text/html",
                    http_status=evidence.proof.http_status,
                    diagnostic=evidence.proof.diagnostic,
                )
                return execution_page, _aware(snapshot.available_at)
            parsed = parse_eastmoney_response(
                request,
                status_code=observation.http_status,
                body=body,
                content_type=observation.mime_type,
            )
            execution_page = ExecutionPage(
                status=parsed.status,
                rows=parsed.rows,
                page_number=parsed.page_number,
                declared_total=parsed.declared_total,
                terminal=parsed.terminal,
                declared_pages=parsed.declared_pages,
                response_sha256=parsed.proof.response_sha256,
                body=body,
                mime_type=observation.mime_type or "application/json",
                http_status=parsed.proof.http_status,
                diagnostic=parsed.proof.diagnostic,
            )
        else:
            try:
                payload = json.loads(body.decode("utf-8"))
                proof = payload["proof"]
                rows = payload["rows"]
                status = ResultStatus(str(proof["status"]))
            except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
                raise StructuredRuntimeError(
                    "saved BaoStock SDK response is not replayable"
                ) from exc
            if proof.get("evidence_kind") != "sdk_result" or proof.get(
                "http_status"
            ) is not None:
                raise StructuredRuntimeError(
                    "saved BaoStock response has invalid SDK proof"
                )
            execution_page = ExecutionPage(
                status=status,
                rows=tuple(dict(row) for row in rows),
                page_number=page_number,
                declared_total=len(rows),
                terminal=bool(proof.get("result_set_exhausted")),
                declared_pages=None,
                response_sha256=str(proof["response_sha256"]),
                body=body,
                mime_type=observation.mime_type or "application/json",
                http_status=None,
                diagnostic=proof.get("diagnostic"),
            )
        return execution_page, _aware(snapshot.available_at)

    def _execute_http(
        self,
        run_id: str,
        dataset: Mapping[str, Any],
        source: FrozenSource,
        params: Mapping[str, Any],
        *,
        purpose: str,
        page_number: int,
        lease_guard: Callable[..., None],
    ) -> ExecutionPage:
        request_contract = dataset["request"]
        from .scope import assert_network_scope, request_fields
        context = self.storage.get_run_context(run_id)
        assert_network_scope(context, dataset["dataset_id"])
        protocol = _PROTOCOLS[request_contract["protocol"]]
        endpoint = _execution_endpoint(dataset, purpose=purpose)
        request_params = request_fields(
            dataset["dataset_id"],
            params,
            dataset.get('research_profile_id'),
            research_profile_id=context.frozen_config.get("research_profile_id"),
        )
        if protocol is ProtocolFamily.EM_F and endpoint.endswith("DateAjaxNew"):
            request_params.pop("dates", None)
            request_params.pop("reportType", None)
        request = EastmoneyRequest(
            protocol=protocol,
            endpoint=endpoint,
            params=request_params,
            max_response_bytes=_MAX_HTTP_BYTES,
        )
        deadline = self.acquisition_runtime.monotonic_clock() + float(
            source.retry_policy.attempt_deadline_seconds
        )
        host = urlsplit(endpoint).hostname
        if not host:
            raise StructuredRuntimeError("structured HTTP endpoint has no host")
        with self.bridge.hold_source(
            run_id=run_id,
            source_definition=source,
            host=host,
            deadline_monotonic=deadline,
            lease_guard=lease_guard,
        ):
            if self._repair_active:
                self._repair_query_io_count += 1
            built = self.acquisition_runtime.http_client.build_request(
                "GET", endpoint, params=request.params
            )
            response = self.acquisition_runtime.http_client.send(built, stream=True)
            try:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    lease_guard()
                    size += len(chunk)
                    if size > _MAX_HTTP_BYTES:
                        raise StructuredRuntimeError(
                            "structured HTTP response exceeded byte limit"
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                content_type = response.headers.get("content-type")
                if purpose == "company_type":
                    evidence = parse_em_f_company_type_response(
                        request,
                        status_code=response.status_code,
                        body=body,
                        content_type=content_type,
                    )
                else:
                    parsed = parse_eastmoney_response(
                        request,
                        status_code=response.status_code,
                        body=body,
                        content_type=content_type,
                    )
            finally:
                response.close()
        if purpose == "company_type":
            rows = (
                ({"code": evidence.company_code, "companyType": evidence.company_type},)
                if evidence.company_type
                else ()
            )
            return ExecutionPage(
                status=evidence.proof.status,
                rows=rows,
                page_number=page_number,
                declared_total=len(rows),
                terminal=True,
                declared_pages=1,
                response_sha256=evidence.proof.response_sha256,
                body=body,
                mime_type=(content_type or "text/html").split(";", 1)[0],
                http_status=evidence.proof.http_status,
                diagnostic=evidence.proof.diagnostic,
            )
        return ExecutionPage(
            status=parsed.status,
            rows=parsed.rows,
            page_number=parsed.page_number,
            declared_total=parsed.declared_total,
            terminal=parsed.terminal,
            declared_pages=parsed.declared_pages,
            response_sha256=parsed.proof.response_sha256,
            body=body,
            mime_type=(content_type or "application/json").split(";", 1)[0],
            http_status=parsed.proof.http_status,
            diagnostic=parsed.proof.diagnostic,
        )

    def _execute_sdk(
        self,
        run_id: str,
        dataset: Mapping[str, Any],
        source: FrozenSource,
        params: Mapping[str, Any],
        *,
        page_number: int,
        lease_guard: Callable[..., None],
    ) -> ExecutionPage:
        if self.sdk is None:
            raise StructuredRuntimeError("BaoStock SDK is not bound")
        from .scope import assert_network_scope
        assert_network_scope(self.storage.get_run_context(run_id), dataset["dataset_id"])
        query = BaoStockQuery(dataset["request"]["endpoint"], params)
        deadline = self.acquisition_runtime.monotonic_clock() + float(
            source.retry_policy.attempt_deadline_seconds
        )
        with self.bridge.hold_source(
            run_id=run_id,
            source_definition=source,
            host="baostock-sdk.local",
            deadline_monotonic=deadline,
            lease_guard=lease_guard,
        ):
            lease_guard(force=True)
            if self._repair_active:
                self._repair_query_io_count += 1
            if self._sdk_session_active:
                result = execute_baostock_query(self.sdk, query)
            else:
                with baostock_anonymous_session(self.sdk):
                    result = execute_baostock_query(self.sdk, query)
            lease_guard(force=True)
        body = canonical_json(
            {
                "method": query.method,
                "params": query.params,
                "rows": result.rows,
                "proof": _jsonable(asdict(result.proof)),
            }
        ).encode("utf-8")
        return ExecutionPage(
            status=result.proof.status,
            rows=result.rows,
            page_number=page_number,
            declared_total=len(result.rows),
            terminal=bool(result.proof.result_set_exhausted),
            declared_pages=None,
            response_sha256=result.proof.response_sha256,
            body=body,
            mime_type="application/json",
            http_status=None,
            diagnostic=result.proof.diagnostic,
        )

    def _project_rows(
        self,
        context: StructuredRunContext,
        job: Mapping[str, Any],
        dataset: Mapping[str, Any],
        page: ExecutionPage,
        snapshot_id: str,
        observed_at: datetime,
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        known_fields = set(
            context.frozen_config.get("known_fields", {}).get(job["dataset_id"], ())
        )
        existing = self.storage.list_records(job_id=job["job_id"], limit=None)
        previous_by_row = {item["row_key"]: item for item in existing}
        records: list[dict[str, Any]] = []
        fields: list[dict[str, Any]] = []
        for ordinal, raw in enumerate(page.rows):
            row = dict(raw)
            # Keep retrieval time locally, outside upstream field projection,
            # business periods and content version hashing.
            row.setdefault("__retrieved_at", observed_at.isoformat())
            key_fields = tuple(dataset["primary_key_fields"])
            if job["purpose"] == "company_type":
                key_fields = ("code",)
            elif job["purpose"] == "report_catalog":
                key_fields = tuple(
                    name
                    for name in ("SECURITY_CODE", "REPORT_DATE", "REPORT_TYPE")
                    if row.get(name) not in (None, "")
                )
                if "REPORT_DATE" not in key_fields:
                    raise StructuredRuntimeError(
                        "EM-F report catalog row has no report date"
                    )
            version = RecordVersion.create(
                dataset_id=job["dataset_id"],
                raw_row=raw,
                key_fields=key_fields,
                snapshot_sha256=page.response_sha256,
                retrieved_at=observed_at,
                available_at=observed_at,
                partition={"scope_key": job["scope_key"]},
            )
            record_id = stable_structured_id(
                "structured-record",
                {"job_id": job["job_id"], "version_key": version.version_key},
            )
            previous = previous_by_row.get(version.row_key)
            records.append(
                {
                    "record_version_id": record_id,
                    "job_id": job["job_id"],
                    "page_id": _page_id(job["job_id"], page),
                    "snapshot_id": snapshot_id,
                    "row_key": version.row_key,
                    "version_hash": version.version_key.removeprefix("version-"),
                    "available_at": observed_at.isoformat(),
                    "observed_at": observed_at.isoformat(),
                    "supersedes_record_version_id": (
                        None if previous is None else previous["record_version_id"]
                    ),
                    "raw_row": row,
                    "row_ordinal": ordinal,
                }
            )
            period_key = next(
                (
                    str(raw[name])
                    for name in dataset.get("date_fields") or ()
                    if raw.get(name) not in (None, "")
                ),
                None,
            )
            for mapped in map_row(
                job["dataset_id"], raw, known_fields=known_fields
            ):
                fields.append(
                    {
                        "field_value_id": stable_structured_id(
                            "structured-field",
                            {
                                "record_version_id": record_id,
                                "raw_field": mapped.raw_field,
                            },
                        ),
                        "record_version_id": record_id,
                        "dataset_id": job["dataset_id"],
                        "raw_field_name": mapped.raw_field,
                        "field_path": f"$.{mapped.raw_field}",
                        "standard_field_id": mapped.standard_field,
                        "definition_version": context.field_registry_version,
                        "nature": mapped.nature.value,
                        "quality": (
                            "passed"
                            if mapped.standard_field is not None
                            else "definition_unknown"
                        ),
                        "value": mapped.raw_value,
                        "unit": mapped.unit,
                        "period_key": period_key,
                    }
                )
        return tuple(records), tuple(fields)

    def _append_attempt_outcome(
        self,
        attempt_id: str,
        lease_epoch: int,
        outcome: AcquisitionOutcome,
        owner_token: str,
        *,
        reason_code: str | None,
        snapshot_ids: Sequence[str] = (),
        protocol_summary: Mapping[str, Any] | None = None,
    ) -> None:
        self.repository.append_attempt_event(
            AcquisitionAttemptEvent(
                event_id=stable_structured_id(
                    "structured-attempt-outcome",
                    {"attempt_id": attempt_id, "outcome": outcome.value},
                ),
                attempt_id=attempt_id,
                event_type=AcquisitionAttemptEventType.OUTCOME_TERMINAL,
                lease_epoch=lease_epoch,
                occurred_at=self.acquisition_runtime.clock(),
                outcome=outcome,
                reason_code=reason_code,
                snapshot_ids=tuple(snapshot_ids),
                protocol_summary={
                    "structured": True,
                    **dict(protocol_summary or {}),
                },
            ),
            owner_token=owner_token,
        )

    def _append_acquisition_coverage(
        self,
        context: StructuredRunContext,
        job: Mapping[str, Any],
        page: ExecutionPage | None,
        snapshot_id: str | None,
        recorded_at: datetime,
        pagination_audit: PageAudit | None = None,
        absent_periods: Sequence[str] = (),
        failure_reason: str | None = None,
        validated_page_ids: Sequence[str] = (),
        completion_attempt_id: str | None = None,
    ) -> None:
        previous = self.storage.list_acquisition_coverage(
            run_id=context.run_id,
            company_id=context.company_id,
            dataset_id=job["dataset_id"],
            limit=None,
        )
        version = max((int(item["version"]) for item in previous), default=0) + 1
        links = self.repository.list_physical_query_coverage_links(
            plan_item_id=job["plan_item_id"]
        )
        row_count = (
            pagination_audit.unique_row_count if pagination_audit is not None
            else len(page.rows) if page is not None else 0
        )
        status = "failed"
        if page is not None and page.status is not ResultStatus.FAILED:
            status = "partial"
            if page.terminal and page.status in {ResultStatus.SUCCESS, ResultStatus.EMPTY}:
                status = "no_data" if row_count == 0 else "complete"
        reason_code = failure_reason or (page.diagnostic if page else None)
        if pagination_audit is not None and not pagination_audit.complete:
            status = "partial"
            pagination_reason = "pagination_incomplete:" + ",".join(
                pagination_audit.issues
            )
            reason_code = (
                f"{reason_code};{pagination_reason}"
                if reason_code
                else pagination_reason
            )
        if absent_periods:
            period_reason = "supplier_period_absent:" + ",".join(
                str(item) for item in absent_periods
            )
            reason_code = (
                f"{reason_code};{period_reason}"
                if reason_code
                else period_reason
            )
        coverage = {
            "coverage_record_id": stable_structured_id(
                "structured-acquisition-coverage",
                {
                    "job_id": job["job_id"],
                    "version": version,
                    "snapshot_id": snapshot_id,
                },
            ),
            "run_id": context.run_id,
            "storage_namespace_id": context.storage_namespace_id,
            "shared_coverage_entry_id": (
                links[0].coverage_entry_id if links else None
            ),
            "job_id": job["job_id"],
            "company_id": context.company_id,
            "dataset_id": job["dataset_id"],
            "scope_key": job["scope_key"],
            "status": status,
            "version": version,
            "safe_through": job.get("time_end") if status in {"complete", "no_data"} else None,
            "query_complete": status in {"complete", "no_data"},
            "completion_rule": "structured-query-completion-v1",
            "response_status": page.status.value if page else "failed",
            "returned_row_count": row_count,
            "snapshot_id": snapshot_id,
            "validated_page_ids": list(validated_page_ids) if status in {"complete", "no_data"} else [],
            "completion_attempt_id": completion_attempt_id,
            "reason_code": reason_code,
            "supersedes_coverage_record_id": (
                previous[-1]["coverage_record_id"] if previous else None
            ),
            "recorded_at": recorded_at.isoformat(),
        }
        if absent_periods:
            coverage["absent_periods"] = [str(item) for item in absent_periods]
        if pagination_audit is not None:
            coverage["pagination_audit"] = asdict(pagination_audit)
        if job.get("reconciles_job_id"):
            original = self.storage.get_job(str(job["reconciles_job_id"]))
            coverage["reconciles_job_id"] = original["job_id"]
            coverage["reconciles_scope_key"] = original["scope_key"]
        self.storage.append_acquisition_coverage(
            coverage
        )

    def _lease_guard(
        self,
        run_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
    ) -> Callable[..., None]:
        last_renewed = [self.acquisition_runtime.monotonic_clock()]

        def guard(*, force: bool = False) -> None:
            current = self.acquisition_runtime.monotonic_clock()
            if force or current - last_renewed[0] >= 30:
                self.repository.renew_lease(
                    run_id,
                    owner_token=owner_token,
                    lease_epoch=lease_epoch,
                    now=self.acquisition_runtime.clock(),
                    ttl_seconds=120,
                )
                last_renewed[0] = current

        return guard

    def _finalize_if_terminal(self, run_id: str, status: Any) -> None:
        if status.pending or status.retryable or status.partial:
            return
        if any(
            event.event_type == AcquisitionRunEventType.FINALIZED
            for event in self.repository.list_run_events(run_id)
        ):
            return
        result = (
            AcquisitionRunResult.SUCCEEDED
            if status.failed == 0 and not getattr(status, "blocked", 0)
            else AcquisitionRunResult.FAILED
            if status.succeeded == 0 and status.no_data == 0
            else AcquisitionRunResult.PARTIAL
        )
        self.repository.append_run_event(
            AcquisitionRunEvent(
                event_id=stable_structured_id(
                    "structured-finalized", {"run_id": run_id, "result": result.value}
                ),
                run_id=run_id,
                event_type=AcquisitionRunEventType.FINALIZED,
                occurred_at=self.acquisition_runtime.clock(),
                result=result,
                coverage_accounted=True,
                material_gap_count=status.failed + getattr(status, "blocked", 0),
                default_consume_eligible=False,
                reason_code="human_acceptance_independent",
                metadata={"structured": True},
            )
        )


def _build_sources(
    bundle: StructuredRegistryBundle,
) -> dict[tuple[str, str], FrozenSource]:
    values: dict[tuple[str, str], FrozenSource] = {}
    intervals = bundle.schedules.source_min_interval_seconds
    for provider in ("eastmoney", "baostock"):
        queries = tuple(
            FrozenQuery(
                query_id=item.dataset_id,
                query_family=item.plan_group_id,
                endpoint=(
                    item.request.endpoint
                    if item.request.method == "GET"
                    else f"baostock+sdk://{item.request.endpoint}"
                ),
                request_method=item.request.method,
                request_encoding=(
                    "sdk" if item.request.method == "SDK" else "query"
                ),
                parameter_template={
                    **item.request.fixed_parameters,
                    **item.request.parameter_template,
                },
                fixed_headers={},
                pagination={"mode": item.pagination},
                parameter_bindings=dict(item.request.parameter_template),
            )
            for item in bundle.datasets.datasets
            if item.provider == provider
        )
        payload = {
            "source_definition_id": f"structured-{provider}",
            "version": bundle.datasets.source_definition_version or bundle.datasets.version,
            "upstream_identity": provider,
            "queries": [item.to_mapping() for item in queries],
            "retry_policy": {
                "max_attempts": bundle.schedules.max_attempts_per_execution,
                "attempt_deadline_seconds": 60.0,
            },
            "rate_limit": {
                "min_interval_seconds": float(intervals[provider])
            },
        }
        source = FrozenSource(
            source_definition_id=payload["source_definition_id"],
            version=payload["version"],
            upstream_identity=provider,
            queries=queries,
            retry_policy=SimpleNamespace(**payload["retry_policy"]),
            rate_limit=SimpleNamespace(**payload["rate_limit"]),
            content_hash=canonical_sha256(payload),
        )
        values[(source.source_definition_id, source.version)] = source
    if sum(len(item.queries) for item in values.values()) != 55:
        raise StructuredRuntimeError("structured runtime must bind exactly 55 queries")
    return values


def _validate_frozen_config(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if value.get("schema") != "structured-execution.v1":
        raise StructuredRuntimeError("unsupported frozen structured context")
    required = {"identity", "datasets", "source_definitions", "known_fields"}
    missing = required - set(value)
    if missing:
        raise StructuredRuntimeError(
            "frozen structured context is incomplete: " + ", ".join(sorted(missing))
        )
    return value


def _execution_endpoint(
    dataset: Mapping[str, Any],
    *,
    purpose: str,
) -> str:
    request = dataset["request"]
    endpoint = str(request["endpoint"])
    if request["protocol"] != "em_f":
        return endpoint
    if purpose == "company_type":
        return (
            "https://emweb.securities.eastmoney.com/PC_HSF10/"
            "NewFinanceAnalysis/Index"
        )
    if purpose == "report_period":
        return endpoint
    if purpose != "report_catalog":
        raise StructuredRuntimeError(f"unknown EM-F work purpose: {purpose}")
    replacements = {
        "zcfzbAjaxNew": "zcfzbDateAjaxNew",
        "lrbAjaxNew": "lrbDateAjaxNew",
        "xjllbAjaxNew": "xjllbDateAjaxNew",
    }
    for source, target in replacements.items():
        if endpoint.endswith(source):
            return endpoint[: -len(source)] + target
    return endpoint


def _bind_parameters(
    dataset: Mapping[str, Any],
    identity: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    page_number: int,
    plan_parameters: Mapping[str, Any] | None = None,
    resolved_company_type: str | None = None,
    selected_report_periods: Sequence[str] = (),
) -> dict[str, Any]:
    market = str(identity["market"])
    exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[market]
    security_code = str(identity["security_code"])
    start = _parse_datetime(job.get("time_start"))
    end = _parse_datetime(job.get("time_end"))
    if start is None or end is None:
        raise StructuredRuntimeError("structured job has no frozen time window")
    base: dict[str, Any] = {
        "security_code": security_code,
        "supplier_security_code": security_code,
        "exchange": exchange,
        "exchange_lower": exchange.lower(),
        "market_number": "1" if market == "SSE" else "0",
        "page_number": page_number,
        "bounded_page_size": int(dataset["request"].get("page_size") or 500),
        "start_date": start.date().isoformat(),
        "end_date": (end - timedelta(microseconds=1)).date().isoformat(),
        "year": start.year,
        "quarter": (start.month - 1) // 3 + 1,
        "catalog_report_dates": "",
        "resolved_company_type": resolved_company_type,
    }
    base["provider_code"] = _provider_code(dataset, identity)
    # BaoStock calls the provider-formatted value ``code`` (for example
    # ``sh.600519``), while EM-F binds the same semantic through
    # ``provider_code``.  Freeze both names explicitly rather than deriving a
    # market from the six-digit prefix during execution.
    base["code"] = base["provider_code"]
    base["provider_secid"] = f"{base['market_number']}.{security_code}"
    concrete = {
        name: value
        for name, value in dict(plan_parameters or {}).items()
        if value is not None and "{" not in str(value) and "}" not in str(value)
    }
    if "report_period" in concrete:
        base["catalog_report_dates"] = concrete["report_period"]
    for name in (
        "year",
        "quarter",
        "catalog_report_dates",
        "resolved_company_type",
    ):
        if name in concrete:
            base[name] = concrete[name]
    if job["purpose"] == "company_type":
        return {"type": "web", "code": base["provider_code"]}
    templates = {
        **(dataset["request"].get("fixed_parameters") or {}),
        **(dataset["request"].get("parameter_template") or {}),
    }
    params: dict[str, Any] = {}
    for name, template in templates.items():
        if name in concrete:
            params[name] = concrete[name]
            continue
        rendered = _render(str(template), base)
        if "{" in rendered or "}" in rendered:
            raise StructuredRuntimeError(
                f"unresolved frozen parameter for {dataset['dataset_id']}: {name}"
            )
        params[name] = rendered
    if dataset.get("research_scope_id"):
        from .scope import request_fields
        params = request_fields(
            dataset["dataset_id"],
            params,
            dataset.get('research_profile_id'),
            research_profile_id=dataset.get("research_scope_profile_id"),
        )
        if "filter" in params:
            date_field = next((f for f in dataset.get("date_fields", []) if f in {"REPORT_DATE", "TRADE_DATE", "END_DATE"}), None)
            if date_field:
                params["filter"] += f"({date_field}>='{base['start_date']}')({date_field}<='{base['end_date']}')"
                if date_field=='REPORT_DATE' and selected_report_periods:
                    dates=','.join("'"+date.fromisoformat(p).isoformat()+"'" for p in selected_report_periods)
                    params['filter']+=f'(REPORT_DATE in ({dates}))'
    return params


def _provider_code(
    dataset: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> str:
    market = str(identity["market"])
    exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[market]
    values = {
        "security_code": str(identity["security_code"]),
        "exchange": exchange,
        "exchange_lower": exchange.lower(),
    }
    provider_format = (
        dataset["request"].get("provider_code_format") or "{security_code}"
    )
    return _render(str(provider_format), values)


def _em_f_statement(dataset: Mapping[str, Any]) -> str:
    endpoint = str(dataset["request"]["endpoint"])
    if endpoint.endswith("zcfzbAjaxNew"):
        return "balance"
    if endpoint.endswith("lrbAjaxNew"):
        return "income"
    if endpoint.endswith("xjllbAjaxNew"):
        return "cashflow"
    raise StructuredRuntimeError(
        f"unknown EM-F statement endpoint: {endpoint}"
    )


def _render(template: str, values: Mapping[str, Any]) -> str:
    result = template
    for name, value in values.items():
        if value is not None:
            result = result.replace("{" + name + "}", str(value))
    return result


def _page_id(job_id: str, page: ExecutionPage) -> str:
    return stable_structured_id(
        "structured-page",
        {
            "job_id": job_id,
            "position": f"page:{page.page_number}",
            "content_hash": page.response_sha256,
        },
    )


def _query_complete(coverage: Mapping[str, Any]) -> bool:
    """Record availability and completion of a query are independent."""
    return bool(coverage.get("safe_through")) and (
        coverage.get("status") == "complete"
        or (
            coverage.get("status") == "no_data"
            and coverage.get("query_complete") is True
        )
    )


def _effective_coverage(rows: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("scope_key") or "")
        current = latest.get(key)
        # Versions are local to a run; cross-run recovery is ordered by time.
        marker = (str(row.get("recorded_at") or ""), int(row.get("version", 0)))
        if current is None or marker > (
            str(current.get("recorded_at") or ""), int(current.get("version", 0))
        ):
            latest[key] = row
    # Reconcile appends its own evidence and explicitly replaces the original
    # window in the cursor view.  The original rows and raw snapshots remain.
    replaced = {
        str(row["reconciles_scope_key"])
        for row in latest.values()
        if row.get("reconciles_scope_key")
        and row["reconciles_scope_key"] != row.get("scope_key")
    }
    return tuple(row for key, row in latest.items() if key not in replaced)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("structured timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _safe_diagnostic(exc: Exception) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ")[:500]
    return f"{type(exc).__name__}:{text}"


def _status_mapping(value: Any) -> dict[str, Any]:
    return {
        "run_id": value.run_id,
        "total_jobs": value.total_jobs,
        "succeeded": value.succeeded,
        "no_data": value.no_data,
        "failed": value.failed,
        "retryable": value.retryable,
        "pending": value.pending,
        "partial": value.partial,
        "blocked": getattr(value, "blocked", 0),
        "jobs": [_jsonable(asdict(item)) for item in value.jobs],
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "FrozenQuery",
    "FrozenSource",
    "StructuredDataRuntime",
    "StructuredPlanResult",
    "StructuredRuntimeError",
]
