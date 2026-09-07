from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .adapters.factory import AcquisitionAdapterFactory
from .bootstrap import ROOT_MARKER_NAME, BootstrapResult, StorageBootstrapper
from .manifests import EvidenceManifestService
from .models import (
    AcquisitionMode,
    AcquisitionPlan,
    AcquisitionRun,
    AcquisitionRunKind,
    CompanyAcquisitionProfile,
    RawResourceSnapshot,
    SourceDefinition,
    SourceRegistry,
    canonical_json_sha256,
)
from .planner import AcquisitionPlanner
from .registry import (
    DEFAULT_QUESTIONS_PATH,
    DEFAULT_REGISTRY_PATH,
    LoadedQuestionSet,
    LoadedSourceRegistry,
    SourceRegistryLoader,
)
from .repository import AcquisitionRepository
from .snapshots import ContentAddressedBlobStore, SnapshotService
from .source_gate import CrossProcessSourceGate


FEATURE_FLAG_ENV = "ASTRAVALUE_ACQUISITION_V1_ENABLED"


class AcquisitionFeatureDisabled(RuntimeError):
    """Raised when an implicit application entrypoint has not enabled v1."""


def acquisition_v1_enabled(value: bool | None = None) -> bool:
    """Resolve the rollout flag; the new write path is disabled by default."""

    if value is not None:
        return bool(value)
    raw = os.environ.get(FEATURE_FLAG_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


def infer_a_share_market(ticker: str) -> str:
    normalized = "".join(char for char in ticker if char.isdigit())
    if len(normalized) != 6:
        raise ValueError("A股ticker必须是6位数字")
    return "SSE" if normalized.startswith(("5", "6", "9")) else "SZSE"


@dataclass(slots=True)
class AcquisitionRuntime:
    """One composition root for one process/invocation and one evidence namespace.

    The runtime owns no global paths.  Every component receives the same bound
    database, data root, frozen registry, namespace, source gate and HTTP client.
    """

    db_path: Path
    data_root: Path
    bootstrap_result: BootstrapResult
    repository: AcquisitionRepository
    loaded_registry: LoadedSourceRegistry
    loaded_questions: LoadedQuestionSet
    planner: AcquisitionPlanner
    adapter_factory: AcquisitionAdapterFactory
    blob_store: ContentAddressedBlobStore
    snapshot_service: SnapshotService
    manifest_service: EvidenceManifestService
    source_gate: CrossProcessSourceGate
    report_storage: Any
    clock: Callable[[], datetime]
    monotonic_clock: Callable[[], float]
    sleeper: Callable[[float], None]
    http_client: httpx.Client
    orchestrator: Any | None = None
    _owns_http_client: bool = True
    _analysis_service: Any | None = field(default=None, repr=False)
    _adapter_manager: Any | None = field(default=None, repr=False)
    _composition_lock: Any = field(default_factory=threading.RLock, repr=False)
    _frozen_definition_cache: dict[
        tuple[str, str, str, tuple[tuple[str, str, str], ...]],
        tuple[SourceDefinition, ...],
    ] = field(default_factory=dict, repr=False)

    @property
    def namespace(self) -> Any:
        return self.bootstrap_result.namespace

    @property
    def namespace_id(self) -> str:
        return str(self.namespace.namespace_id)

    @classmethod
    def create(
        cls,
        db_path: Path | str,
        data_root: Path | str,
        *,
        registry_path: Path | str = DEFAULT_REGISTRY_PATH,
        questions_path: Path | str = DEFAULT_QUESTIONS_PATH,
        workspace_root: Path | str | None = None,
        adapter_factory: AcquisitionAdapterFactory | None = None,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        bootstrapper: StorageBootstrapper | None = None,
        busy_timeout_ms: int = 5_000,
        orchestrator_factory: Callable[["AcquisitionRuntime"], Any] | None = None,
    ) -> "AcquisitionRuntime":
        database = Path(db_path).expanduser().resolve()
        root = Path(data_root).expanduser().resolve()
        factory = adapter_factory or AcquisitionAdapterFactory()
        loader = SourceRegistryLoader(
            known_adapter_keys={
                *factory.installed_adapter_keys,
                "official",
                "akshare",
                "sina",
                "baostock",
                "tushare",
            }
        )
        questions = loader.load_questions(questions_path)
        registry = loader.load_registry(
            registry_path,
            question_set=questions,
            expect_business_model_v1=4,
        )
        bootstrap = bootstrapper or StorageBootstrapper(
            database,
            root,
            busy_timeout_ms=busy_timeout_ms,
        )
        result = bootstrap.bootstrap()
        from ..storage import ReportStorage

        report_storage = ReportStorage(
            database,
            migration_data_root=root,
            busy_timeout_ms=busy_timeout_ms,
        )
        repository = AcquisitionRepository(
            database,
            busy_timeout_ms=busy_timeout_ms,
            initialize=False,
            data_root=root,
        )
        repository.save_source_registry_version(registry.registry)
        for definition in (
            *registry.registry.definitions,
            *registry.registry.legacy_definitions,
        ):
            repository.save_source_definition_version(definition)

        blob_store = ContentAddressedBlobStore(
            root,
            result.namespace,
            binding_validator=_validate_bound_marker,
        )
        snapshot_service = SnapshotService(blob_store, repository)
        policy_index = {
            (item.source_definition_id, str(item.version)): item
            for item in (
                *registry.registry.definitions,
                *registry.registry.legacy_definitions,
            )
        }

        def resolve_policy(definition_id: str, version: str) -> SourceDefinition:
            try:
                return policy_index[(definition_id, str(version))]
            except KeyError:
                definition = repository.get_source_definition_version(
                    definition_id, version
                )
                if not isinstance(definition, SourceDefinition):
                    definition = SourceDefinition.model_validate(definition)
                return definition

        manifest_service = EvidenceManifestService(
            blob_store,
            repository,
            resolve_policy,
        )
        workspace = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else Path(__file__).resolve().parents[3]
        )
        bound_clock = clock or (lambda: datetime.now(timezone.utc))
        bound_monotonic = monotonic_clock or time.monotonic
        bound_sleeper = sleeper or time.sleep
        owns_http_client = http_client is None
        bound_http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            trust_env=False,
        )
        runtime = cls(
            db_path=database,
            data_root=root,
            bootstrap_result=result,
            repository=repository,
            loaded_registry=registry,
            loaded_questions=questions,
            planner=AcquisitionPlanner(registry, questions),
            adapter_factory=factory,
            blob_store=blob_store,
            snapshot_service=snapshot_service,
            manifest_service=manifest_service,
            source_gate=CrossProcessSourceGate(workspace),
            report_storage=report_storage,
            clock=bound_clock,
            monotonic_clock=bound_monotonic,
            sleeper=bound_sleeper,
            http_client=bound_http_client,
            _owns_http_client=owns_http_client,
        )
        if orchestrator_factory is None:
            try:
                from .orchestrator import AcquisitionOrchestrator
            except ImportError:
                AcquisitionOrchestrator = None  # type: ignore[assignment]
            if AcquisitionOrchestrator is not None:
                runtime.orchestrator = AcquisitionOrchestrator(
                    runtime,
                    now=runtime.clock,
                    monotonic=runtime.monotonic_clock,
                    sleep=runtime.sleeper,
                )
        else:
            runtime.orchestrator = orchestrator_factory(runtime)
        return runtime

    @property
    def analysis_service(self) -> Any:
        """Return the one legacy-service facade bound to this runtime's store."""

        with self._composition_lock:
            if self._analysis_service is None:
                from ..service import AnalysisService

                self._analysis_service = AnalysisService(storage=self.report_storage)
            return self._analysis_service

    @property
    def adapter_manager(self) -> Any:
        """Return the one compatibility manager wired to this runtime."""

        with self._composition_lock:
            if self._adapter_manager is None:
                from ..adapters.manager import AdapterManager

                self._adapter_manager = AdapterManager(
                    loaded_registry=self.loaded_registry,
                    acquisition_runtime=self,
                )
            return self._adapter_manager

    def bind_analysis_service(self, service: Any) -> Any:
        """Adopt an explicitly supplied facade only when it uses our store."""

        if getattr(service, "storage", None) is not self.report_storage:
            raise ValueError(
                "AnalysisService必须复用AcquisitionRuntime绑定的ReportStorage实例"
            )
        with self._composition_lock:
            if self._analysis_service not in (None, service):
                raise ValueError("AcquisitionRuntime已绑定另一AnalysisService实例")
            self._analysis_service = service
        return service

    def source_definition(self, definition_id: str, version: str | None = None) -> SourceDefinition:
        matches = [
            item
            for item in (
                *self.loaded_registry.registry.definitions,
                *self.loaded_registry.registry.legacy_definitions,
            )
            if item.source_definition_id == definition_id
            and (version is None or str(item.version) == str(version))
        ]
        if len(matches) != 1:
            suffix = "" if version is None else f"@{version}"
            raise KeyError(f"来源定义不存在或不唯一: {definition_id}{suffix}")
        return matches[0]

    def frozen_source_definitions(
        self, run: AcquisitionRun
    ) -> tuple[SourceDefinition, ...]:
        """Resolve and verify the exact registry/source versions frozen by a run.

        Repository state is authoritative for an existing run.  The current
        checkout's registry is intentionally not consulted here because a
        later registry release may no longer contain an older source version.
        """

        cache_key = (
            run.registry_id,
            str(run.registry_version),
            run.registry_content_hash,
            tuple(
                (
                    item.source_definition_id,
                    str(item.version),
                    item.content_hash,
                )
                for item in run.source_definition_refs
            ),
        )
        with self._composition_lock:
            cached = self._frozen_definition_cache.get(cache_key)
            if cached is not None:
                return cached
            try:
                frozen_registry = self.repository.get_source_registry_version(
                    run.registry_id, run.registry_version
                )
            except Exception as exc:
                raise ValueError(
                    "run固定的来源注册表版本无法从repository恢复: "
                    f"{run.registry_id}@{run.registry_version}"
                ) from exc
            if not isinstance(frozen_registry, SourceRegistry):
                frozen_registry = SourceRegistry.model_validate(frozen_registry)
            if (
                frozen_registry.registry_id != run.registry_id
                or str(frozen_registry.registry_version)
                != str(run.registry_version)
            ):
                raise ValueError("run固定的来源注册表id/version不匹配")
            if canonical_json_sha256(frozen_registry) != run.registry_content_hash:
                raise ValueError("run固定的来源注册表content hash不匹配")

            registry_definitions = {
                (item.source_definition_id, str(item.version)): item
                for item in (
                    *frozen_registry.definitions,
                    *frozen_registry.legacy_definitions,
                )
            }
            resolved: list[SourceDefinition] = []
            for reference in run.source_definition_refs:
                identity = (reference.source_definition_id, str(reference.version))
                registry_definition = registry_definitions.get(identity)
                if registry_definition is None:
                    raise ValueError(
                        "run固定的来源定义不在其冻结注册表: "
                        f"{reference.source_definition_id}@{reference.version}"
                    )
                try:
                    stored_definition = self.repository.get_source_definition_version(
                        *identity
                    )
                except Exception as exc:
                    raise ValueError(
                        "run固定的来源定义版本无法从repository恢复: "
                        f"{reference.source_definition_id}@{reference.version}"
                    ) from exc
                if not isinstance(stored_definition, SourceDefinition):
                    stored_definition = SourceDefinition.model_validate(
                        stored_definition
                    )
                expected_hash = reference.content_hash
                if (
                    canonical_json_sha256(registry_definition) != expected_hash
                    or canonical_json_sha256(stored_definition) != expected_hash
                    or stored_definition != registry_definition
                ):
                    raise ValueError(
                        "run固定的来源定义content hash不匹配: "
                        f"{reference.source_definition_id}@{reference.version}"
                    )
                resolved.append(stored_definition)
            result = tuple(resolved)
            self._frozen_definition_cache[cache_key] = result
            return result

    def source_definition_for_run(
        self,
        run: AcquisitionRun,
        definition_id: str,
        version: str,
    ) -> SourceDefinition:
        matches = [
            item
            for item in self.frozen_source_definitions(run)
            if item.source_definition_id == definition_id
            and str(item.version) == str(version)
        ]
        if len(matches) != 1:
            raise KeyError(
                f"run未固定来源定义: {definition_id}@{version}"
            )
        return matches[0]

    def snapshot_bytes(self, snapshot_id: str) -> bytes:
        snapshot = self.repository.get_raw_resource_snapshot(snapshot_id)
        if not isinstance(snapshot, RawResourceSnapshot):
            snapshot = RawResourceSnapshot.model_validate(snapshot)
        return self.blob_store.read_verified(
            snapshot.archive_relative_path,
            expected_sha256=snapshot.sha256,
            expected_length=snapshot.byte_length,
        )

    def build_profile(
        self,
        ticker: str,
        *,
        company_name: str | None = None,
        market: str | None = None,
        listing_date: date | None = None,
        prospectus_date: date | None = None,
        fallback_earliest_date: date | None = None,
        fallback_reason: str | None = None,
    ) -> CompanyAcquisitionProfile:
        normalized = "".join(char for char in ticker if char.isdigit())
        selected_market = market or infer_a_share_market(normalized)
        if listing_date is None and prospectus_date is None and fallback_earliest_date is None:
            candidates = [
                query.earliest_available_at.date()
                for definition in self.loaded_registry.registry.definitions
                if "business_model" in definition.scopes
                and definition.applies_to(normalized, selected_market)
                for query in definition.queries
                if query.earliest_available_at is not None
            ]
            if not candidates:
                raise ValueError("缺少可审计的baseline历史起点")
            fallback_earliest_date = min(candidates)
            fallback_reason = (
                fallback_reason
                or "运行创建时缺少已冻结公司锚点，使用固定注册表的适用来源最早可得日"
            )
        return CompanyAcquisitionProfile(
            ticker=normalized,
            company_name=company_name or normalized,
            market=selected_market,
            listing_date=listing_date,
            prospectus_date=prospectus_date,
            fallback_earliest_date=fallback_earliest_date,
            fallback_reason=fallback_reason,
        )

    def create_plan(
        self,
        profile: CompanyAcquisitionProfile,
        *,
        mode: AcquisitionMode | str,
        as_of: datetime | None = None,
        run_kind: AcquisitionRunKind | str = AcquisitionRunKind.PRODUCTION,
        start_at: datetime | None = None,
        parent_run_id: str | None = None,
        reconcile_target: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> AcquisitionPlan:
        plan = self.planner.plan(
            profile,
            mode=mode,
            as_of=as_of or self.clock(),
            run_kind=run_kind,
            start_at=start_at,
            parent_run_id=parent_run_id,
            reconcile_target=reconcile_target,
            storage_namespace_id=self.namespace_id,
        )
        if persist:
            self.repository.save_plan_bundle(
                plan.run,
                plan.physical_query_plan_items,
                plan.coverage_entries,
                plan.coverage_links,
            )
        return plan

    def plan_company_run(
        self,
        ticker: str,
        *,
        mode: AcquisitionMode | str,
        as_of: datetime | None = None,
        company_name: str | None = None,
        market: str | None = None,
        listing_date: date | None = None,
        prospectus_date: date | None = None,
        run_kind: AcquisitionRunKind | str = AcquisitionRunKind.PRODUCTION,
        parent_run_id: str | None = None,
        persist: bool = True,
    ) -> AcquisitionPlan:
        """Resolve checkpoint/reconcile inputs and create a durable plan.

        Production callers deliberately cannot pass a time or question subset.
        Incremental and reconcile ranges are derived from committed state.
        """

        selected_mode = AcquisitionMode(mode)
        selected_kind = AcquisitionRunKind(run_kind)
        cutoff = as_of or self.clock()
        profile = self.build_profile(
            ticker,
            company_name=company_name,
            market=market,
            listing_date=listing_date,
            prospectus_date=prospectus_date,
        )
        start_at: datetime | None = None
        target: dict[str, Any] | None = None
        resolved_parent = parent_run_id
        if selected_mode == AcquisitionMode.INCREMENTAL:
            start_at = self._incremental_start(profile)
        elif selected_mode == AcquisitionMode.RECONCILE:
            from .reconcile import select_reconcile_target
            excluded = []
            if resolved_parent is None:
                offset = 0
                while resolved_parent is None:
                    candidates = self.repository.list_runs(
                        ticker=profile.ticker, run_kind=AcquisitionRunKind.PRODUCTION.value,
                        limit=100, offset=offset,
                    )
                    for candidate in candidates:
                        if (candidate.mode == AcquisitionMode.RECONCILE or
                                candidate.question_set_id != self.loaded_questions.question_set.question_set_id):
                            continue
                        if any(event.event_type.value == "finalized" for event in
                               self.repository.list_run_events(candidate.run_id)):
                            resolved_parent = candidate.run_id
                            break
                        excluded.append({"run_id": candidate.run_id, "reason": "parent_not_finalized"})
                    if len(candidates) < 100:
                        break
                    offset += len(candidates)
                if resolved_parent is None:
                    raise ValueError("reconcile找不到同公司可关联的父运行")
            parent = self.repository.get_run(resolved_parent)
            if (parent.ticker != profile.ticker or parent.run_kind != AcquisitionRunKind.PRODUCTION
                    or parent.question_set_id != self.loaded_questions.question_set.question_set_id):
                raise ValueError("reconcile父运行ticker/scope不匹配")
            selection = select_reconcile_target(
                self.repository, self.loaded_registry.registry.definitions,
                resolved_parent, as_of=cutoff, now=self.clock(),
                frozen_definitions=self.frozen_source_definitions(parent),
            )
            start_at = selection.start_at
            target = {**selection.target, "excluded_newer_unfinalized_runs": excluded}
        return self.create_plan(
            profile,
            mode=selected_mode,
            as_of=cutoff,
            run_kind=selected_kind,
            start_at=start_at,
            parent_run_id=resolved_parent,
            reconcile_target=target,
            persist=persist,
        )

    def default_business_model_mode(
        self,
        ticker: str,
        *,
        market: str | None = None,
    ) -> AcquisitionMode:
        """Select baseline until every applicable source has a safe checkpoint.

        This compatibility helper never treats the mere presence of an older
        or failed run as incremental eligibility.  The same conservative
        checkpoint check used by explicit incremental planning is authoritative.
        """

        profile = self.build_profile(ticker, market=market)
        try:
            self._incremental_start(profile)
        except ValueError as exc:
            if "不存在兼容且安全的checkpoint" not in str(exc):
                raise
            return AcquisitionMode.BASELINE
        return AcquisitionMode.INCREMENTAL

    def _incremental_start(self, profile: CompanyAcquisitionProfile) -> datetime:
        starts: list[datetime] = []
        missing: list[str] = []
        for definition in self.loaded_registry.registry.definitions:
            if "business_model" not in definition.scopes:
                continue
            if not definition.enabled or not definition.applies_to(profile.ticker, profile.market):
                continue
            if definition.collection_role == "on_demand":
                continue
            checkpoint = self.repository.latest_checkpoint(
                profile.ticker,
                definition.source_definition_id,
                definition.version,
                self.loaded_questions.question_set.version,
            )
            if checkpoint is None or checkpoint.source_safe_through is None:
                missing.append(definition.source_definition_id)
                continue
            starts.append(
                checkpoint.source_safe_through.time_upper_bound
                - timedelta(days=definition.incremental_policy.overlap_days)
            )
        if missing or not starts:
            detail = ", ".join(sorted(missing)) or "全部适用来源"
            raise ValueError(f"不存在兼容且安全的checkpoint，需要baseline或reconcile: {detail}")
        return min(starts)

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    def __enter__(self) -> "AcquisitionRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _validate_bound_marker(root: Path, namespace_id: str) -> None:
    marker_path = root / ROOT_MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("data_root缺少有效storage namespace marker") from exc
    if marker.get("state") != "bound" or marker.get("namespace_id") != namespace_id:
        raise ValueError("data_root与storage namespace不匹配")


__all__ = [
    "AcquisitionFeatureDisabled",
    "AcquisitionRuntime",
    "FEATURE_FLAG_ENV",
    "acquisition_v1_enabled",
    "infer_a_share_market",
]
