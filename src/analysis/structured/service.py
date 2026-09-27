from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from analysis.acquisition.repository import (
    AcquisitionNotFoundError,
    LeaseConflictError,
    StorageBusyError,
)
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.exports import export_report
from analysis.service import AnalysisService

from .exceptions import (
    StructuredBusyError,
    StructuredConflictError,
    StructuredIntegrityError,
)
from .identity import CompanyResolver, ResolutionStatus, SecurityIdentity
from .materialization import MaterializationResult, StructuredFactMaterializer
from .registry import (
    DEFAULT_CONFIG_DIR,
    StructuredRegistryBundle,
    StructuredRegistryLoader,
)
from .repair import load_repair_manifest, write_repair_manifest
from .runtime import StructuredDataRuntime, StructuredPlanResult
from .storage import (
    StructuredNamespaceMismatch,
    StructuredSchemaError,
    StructuredStorageError,
)


class StructuredDataService:
    """Application facade over one bound acquisition/structured runtime.

    ``from_runtime`` is the composition path for API processes that already own
    an :class:`AcquisitionRuntime`; it deliberately performs no second
    acquisition bootstrap.  ``create`` is retained for standalone CLI use and
    closes only the acquisition runtime it created itself.
    """

    def __init__(
        self,
        runtime: StructuredDataRuntime,
        *,
        identities: Iterable[SecurityIdentity],
        owns_acquisition_runtime: bool,
    ) -> None:
        self.runtime = runtime
        self.bundle = runtime.bundle
        self.storage = runtime.storage
        self.acquisition_runtime = runtime.acquisition_runtime
        self._owns_acquisition_runtime = owns_acquisition_runtime
        self._identities = tuple(identities)
        self.resolver = CompanyResolver(self._identities)

    @classmethod
    def create(
        cls,
        db_path: Path | str,
        data_root: Path | str,
        *,
        config_dir: Path | str = DEFAULT_CONFIG_DIR,
        identities: Iterable[SecurityIdentity] = (),
        sdk: Any | None = None,
        **runtime_kwargs: Any,
    ) -> "StructuredDataService":
        acquisition_runtime = AcquisitionRuntime.create(
            db_path,
            data_root,
            **runtime_kwargs,
        )
        try:
            return cls.from_runtime(
                acquisition_runtime,
                registry_bundle=StructuredRegistryLoader(config_dir).load(),
                identities=identities,
                sdk=sdk,
                _owns_acquisition_runtime=True,
            )
        except Exception:
            acquisition_runtime.close()
            raise

    @classmethod
    def from_runtime(
        cls,
        acquisition_runtime: AcquisitionRuntime,
        *,
        registry_bundle: StructuredRegistryBundle | None = None,
        identities: Iterable[SecurityIdentity] = (),
        sdk: Any | None = None,
        _owns_acquisition_runtime: bool = False,
    ) -> "StructuredDataService":
        bundle = registry_bundle or StructuredRegistryLoader().load()
        merged = _merge_identities(_peer_identities(bundle), identities)
        runtime = StructuredDataRuntime(
            acquisition_runtime,
            registry_bundle=bundle,
            sdk=sdk,
        )
        return cls(
            runtime,
            identities=merged,
            owns_acquisition_runtime=_owns_acquisition_runtime,
        )

    def close(self) -> None:
        if self._owns_acquisition_runtime:
            self.acquisition_runtime.close()
            self._owns_acquisition_runtime = False

    def __enter__(self) -> "StructuredDataService":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def registry(self, *, limit: int = 500, offset: int = 0) -> dict[str, Any]:
        values = [
            item.model_dump(mode="json") for item in self.bundle.datasets.datasets
        ]
        return _page(values, limit=limit, offset=offset)

    def fields(
        self,
        *,
        dataset_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        values = [
            item.model_dump(mode="json")
            for item in self.bundle.fields.fields
            if dataset_id is None or item.dataset_id == dataset_id
        ]
        return _page(values, limit=limit, offset=offset)

    def resolve_company(
        self,
        query: str,
        *,
        market: str | None = None,
        as_of: date | datetime | None = None,
    ) -> dict[str, Any]:
        resolution = self.resolver.resolve(
            query,
            as_of=_as_date(as_of),
            market=market,
        )
        return _resolution_mapping(resolution)

    def industry_profile(self, ticker: str) -> dict[str, Any]:
        resolution = self.resolver.resolve(ticker, as_of=_as_date(None))
        return {
            "ticker": ticker,
            "company_resolution": _resolution_mapping(resolution),
            "status": "pending",
            "profile_ids": [],
            "reason_codes": ["industry_profile_evidence_missing"],
            "general_fallback_used": False,
            "performed_network_io": False,
        }

    def peer_candidates(
        self,
        ticker: str,
        *,
        company_scope: str = "company-with-peers",
    ) -> dict[str, Any]:
        if company_scope not in {
            "company-only",
            "company-with-peers",
            "peer-set",
        }:
            raise ValueError("unknown structured company scope")
        resolution = self.resolver.resolve(ticker, as_of=_as_date(None))
        selected: list[dict[str, Any]] = []
        if resolution.status is ResolutionStatus.RESOLVED and resolution.identity:
            identities = self._scope_identities(resolution.identity, company_scope)
            selected = [
                {
                    **_identity_mapping(item),
                    "selected": item.security_id != resolution.identity.security_id,
                    "decision_kind": "rule_selected",
                    "reason_codes": ["configured_peer_set"],
                }
                for item in identities
                if item.security_id != resolution.identity.security_id
            ]
        return {
            "ticker": ticker,
            "company_scope": company_scope,
            "company_resolution": _resolution_mapping(resolution),
            "total": len(selected),
            "items": selected,
            "recursive_expansion": False,
            "performed_network_io": False,
        }

    def plan(
        self,
        ticker: str,
        *,
        mode: str = "baseline",
        company_scope: str = "company-only",
        datasets: Sequence[str] | None = None,
        as_of: date | datetime | None = None,
        valuation_start: date | None = None,
        report_periods: Sequence[str] = (),
        industry_profile_id: str | None = None,
        research_profile_id: str | None = None,
    ) -> dict[str, Any]:
        cutoff = _as_datetime(as_of, fallback=self.acquisition_runtime.clock())
        resolution = self.resolver.resolve(ticker, as_of=cutoff.date())
        if resolution.status is not ResolutionStatus.RESOLVED or resolution.identity is None:
            return {
                "plan_id": None,
                "ticker": ticker,
                "mode": mode,
                "company_scope": company_scope,
                "runnable": False,
                "persisted": False,
                "performed_network_io": False,
                "identity_prerequisites": list(
                    resolution.prerequisite_dataset_ids
                ),
                "company_resolution": _resolution_mapping(resolution),
            }
        identities = self._scope_identities(resolution.identity, company_scope)
        result = self.runtime.plan(
            identities,
            mode=mode,
            company_scope=company_scope,
            dataset_ids=None if not datasets else tuple(datasets),
            valuation_start=valuation_start,
            report_periods=report_periods,
            industry_profile_id=industry_profile_id,
            research_profile_id=research_profile_id,
            as_of=cutoff,
        )
        return {
            **result.to_mapping(),
            "ticker": resolution.identity.canonical_ticker,
            "runnable": True,
            "company_resolution": _resolution_mapping(resolution),
        }

    def run(self, run_id: str) -> dict[str, Any]:
        return self._execute(self.runtime.execute, run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        return self._execute(self.runtime.resume, run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        return self._execute(self.runtime.status, run_id)

    def materialize(
        self,
        run_id: str,
        *,
        as_of: datetime | None = None,
        strict_historical: bool = False,
        persist: bool = True,
        include_records: bool = True,
        interpretation_contract: str | None = None,
        research_scope: bool = True,
        research_profile_id: str | None = None,
    ) -> dict[str, Any]:
        """Turn committed structured rows into the report fact projection.

        Materialization is read-only with respect to the structured control
        plane.  The optional report projection uses the same bound database as
        the acquisition runtime, so a report can consume the result without a
        second store or a second source of truth.
        """
        result: MaterializationResult = StructuredFactMaterializer(
            self.storage, self.runtime.repository
        ).materialize(
            run_id,
            as_of=as_of,
            strict_historical=strict_historical,
            interpretation_contract=interpretation_contract,
            research_scope=research_scope,
            research_profile_id=research_profile_id,
        )
        projection = None
        if persist:
            report_storage = self.acquisition_runtime.report_storage
            if report_storage.db_path.resolve() != self.storage.db_path.resolve():
                raise StructuredIntegrityError("materialization report database binding mismatch")
            report_storage.save_sources(list(result.sources))
            report_storage.save_facts(list(result.facts))
            report_storage.save_dimensional_facts(list(result.dimensional_facts))
            report_storage.save_events(list(result.events))
            from analysis.timeseries import TimeSeriesStore
            timeseries = TimeSeriesStore(
                report_storage.db_path.with_suffix(".duckdb"),
                report_storage.db_path.parent / "parquet")
            timeseries.append_facts(list(result.facts))
            timeseries.append_research_records(
                dimensional_facts=list(result.dimensional_facts), events=list(result.events))
            projection = timeseries.export_materialization(result)
            # Published only after every projection succeeds. A failed partial
            # write is safely retryable; it never advertises a completed manifest.
            report_storage.save_materialization(result, projection)
        summary = {
            **result.to_mapping(),
            "persisted": persist,
            "projection": projection,
        }
        if not include_records:
            return summary
        return {
            **summary,
            "facts": [item.model_dump(mode="json") for item in result.facts],
            "dimensional_facts": [item.model_dump(mode="json") for item in result.dimensional_facts],
            "events": [item.model_dump(mode="json") for item in result.events],
            "field_gaps": list(result.field_gaps),
            "sources": [item.model_dump(mode="json") for item in result.sources],
        }

    def report(
        self,
        pack_dir: Path | str,
        *,
        output_dir: Path | str,
        formats: Sequence[str] = ("md", "html", "xlsx", "pdf"),
        industry: str | None = None,
    ) -> dict[str, Any]:
        """从冻结轻量包生成一个持久报告版本及同源导出。"""

        normalized_formats = tuple(dict.fromkeys(str(item).lower() for item in formats))
        unsupported = sorted(set(normalized_formats) - {"md", "html", "xlsx", "pdf"})
        if unsupported or not normalized_formats:
            raise ValueError(
                "structured report仅支持md、html、xlsx和pdf"
                + (f": {', '.join(unsupported)}" if unsupported else "")
            )
        report_storage = self.acquisition_runtime.report_storage
        if report_storage.db_path.resolve() != self.storage.db_path.resolve():
            raise StructuredIntegrityError("structured report database binding mismatch")
        from .reporting_bridge import build_report_request

        request = build_report_request(
            pack_dir,
            industry=industry,
            source_lookup=report_storage.get_source,
        )
        report = AnalysisService(storage=report_storage).create_report(request)
        root = Path(output_dir).resolve()
        target = root / report.ticker / f"v{report.version}-{report.report_id[:8]}"
        target.mkdir(parents=True, exist_ok=False)
        report_json = target / "report.json"
        report_json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        paths = {"json": report_json}
        for format_name in normalized_formats:
            paths[format_name] = export_report(report, format_name, target)

        from .storage import canonical_json
        from .research_lite import _hash_file

        outputs = {
            name: {
                "path": str(path),
                "relative_path": path.relative_to(root).as_posix(),
                "sha256": _hash_file(path),
                "bytes": path.stat().st_size,
            }
            for name, path in paths.items()
        }
        manifest = {
            "schema": "structured-report-output.v1",
            "status": "created",
            "report_id": report.report_id,
            "report_version": report.version,
            "ticker": report.ticker,
            "company_name": report.company_name,
            "industry": report.industry,
            "industry_route": report.request_metadata.get("industry_route"),
            "data_snapshot_id": report.data_snapshot_id,
            "research_coverage_snapshot_id": report.research_coverage_snapshot_id,
            "method_bundle_id": report.method_bundle_id,
            "lite_pack_id": report.request_metadata.get("lite_pack_id"),
            "lite_pack_identity_hash": report.request_metadata.get(
                "lite_pack_identity_hash"
            ),
            "materialization_selected_fact_count": len(
                report.materialization_selected_fact_ids
            ),
            "fact_count": len(report.facts),
            "dimensional_fact_count": len(report.dimensional_facts),
            "rating": report.conclusion.rating.value,
            "rating_confirmed": report.conclusion.rating_confirmed,
            "performed_network_io": False,
            "outputs": outputs,
        }
        manifest_path = target / "report-manifest.json"
        manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        return {
            **manifest,
            "output_dir": str(target),
            "manifest": {
                "path": str(manifest_path),
                "relative_path": manifest_path.relative_to(root).as_posix(),
                "sha256": _hash_file(manifest_path),
                "bytes": manifest_path.stat().st_size,
            },
        }

    def repair_plan(
        self,
        run_id: str,
        *,
        datasets: Sequence[str],
        reasons: Sequence[str],
        code_revision: str,
        output: Path | str,
    ) -> dict[str, Any]:
        manifest = self.runtime.repair_plan(
            run_id,
            dataset_filters=tuple(datasets),
            reason_filters=tuple(reasons),
            code_revision=code_revision,
        )
        write_repair_manifest(output, manifest)
        return {
            **manifest.model_dump(mode="json"),
            "target_count": len(manifest.items),
            "manifest_written": True,
            "performed_network_io": False,
        }

    def repair_run(
        self,
        manifest_path: Path | str,
        *,
        code_revision: str,
        max_jobs_per_round: int = 25,
    ) -> dict[str, Any]:
        manifest = load_repair_manifest(manifest_path)
        try:
            return self.runtime.repair_execute(
                manifest,
                expected_code_revision=code_revision,
                max_jobs_per_round=max_jobs_per_round,
            )
        except (LeaseConflictError, StorageBusyError) as exc:
            raise StructuredBusyError(str(exc)) from exc
        except StructuredSchemaError as exc:
            raise StructuredIntegrityError(str(exc)) from exc
        except (StructuredNamespaceMismatch, StructuredStorageError) as exc:
            raise StructuredConflictError(str(exc)) from exc

    def repair_status(
        self,
        manifest_path: Path | str,
        *,
        code_revision: str | None = None,
    ) -> dict[str, Any]:
        manifest = load_repair_manifest(manifest_path)
        return self.runtime.repair_status(
            manifest, expected_code_revision=code_revision
        )

    def records(
        self,
        *,
        run_id: str | None = None,
        dataset_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        job_ids = self._matching_job_ids(run_id=run_id, dataset_id=dataset_id)
        values = [
            item
            for job_id in job_ids
            for item in self.storage.list_records(job_id=job_id, limit=None)
        ]
        values.sort(
            key=lambda item: (
                str(item.get("job_id", "")),
                str(item.get("row_key", "")),
                str(item.get("available_at", "")),
                str(item.get("record_version_id", "")),
            )
        )
        return _page(values, limit=limit, offset=offset)

    def reading_tasks(
        self,
        *,
        run_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        run_ids = (run_id,) if run_id is not None else self._structured_run_ids()
        values: list[dict[str, Any]] = []
        for current_run_id in run_ids:
            try:
                values.extend(
                    self.storage.list_reading_tasks(
                        run_id=current_run_id,
                        limit=None,
                    )
                )
            except AcquisitionNotFoundError:
                continue
        values.sort(
            key=lambda item: (
                str(item.get("created_at", "")),
                str(item.get("reading_task_id", "")),
            )
        )
        return _page(values, limit=limit, offset=offset)

    def coverage(
        self,
        snapshot_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        snapshot = self.storage.get_research_coverage_snapshot(snapshot_id)
        values = self.storage.list_requirement_evaluations(
            snapshot_id,
            limit=None,
        )
        return {
            **snapshot,
            **_page(values, limit=limit, offset=offset),
        }

    def acquisition_coverage(
        self,
        *,
        run_id: str | None = None,
        company_id: str | None = None,
        dataset_id: str | None = None,
        status: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        values = self.storage.list_acquisition_coverage(
            run_id=run_id,
            company_id=company_id,
            dataset_id=dataset_id,
            status=status,
            limit=None,
        )
        return _page(values, limit=limit, offset=offset)

    def _scope_identities(
        self,
        target: SecurityIdentity,
        company_scope: str,
    ) -> tuple[SecurityIdentity, ...]:
        if company_scope == "company-only":
            return (target,)
        if company_scope not in {"company-with-peers", "peer-set"}:
            raise ValueError("unknown structured company scope")
        for peer_set in self.bundle.peer_sets.peer_sets:
            peer_ids = {
                item.canonical_ticker.upper() for item in peer_set.companies
            }
            if target.canonical_ticker not in peer_ids:
                continue
            configured = tuple(
                identity
                for identity in self._identities
                if identity.canonical_ticker in peer_ids
            )
            if company_scope == "company-with-peers":
                peers = tuple(
                    item
                    for item in configured
                    if item.security_id != target.security_id
                )[:6]
                return (target, *peers)
            return configured
        if company_scope == "peer-set":
            raise ValueError("target is not a member of a configured peer set")
        return (target,)

    def _structured_run_ids(self) -> tuple[str, ...]:
        values: list[str] = []
        offset = 0
        while True:
            runs = self.runtime.repository.list_runs(limit=500, offset=offset)
            if not runs:
                break
            for run in runs:
                try:
                    self.storage.get_run_context(run.run_id)
                except AcquisitionNotFoundError:
                    continue
                values.append(str(run.run_id))
            offset += len(runs)
        return tuple(values)

    def _matching_job_ids(
        self,
        *,
        run_id: str | None,
        dataset_id: str | None,
    ) -> tuple[str, ...]:
        run_ids = (run_id,) if run_id is not None else self._structured_run_ids()
        values: list[str] = []
        for current_run_id in run_ids:
            try:
                jobs = self.storage.list_jobs(current_run_id, limit=None)
            except AcquisitionNotFoundError:
                continue
            values.extend(
                str(item["job_id"])
                for item in jobs
                if dataset_id is None or item["dataset_id"] == dataset_id
            )
        return tuple(values)

    @staticmethod
    def _execute(call: Any, run_id: str) -> dict[str, Any]:
        try:
            return call(run_id)
        except (LeaseConflictError, StorageBusyError) as exc:
            raise StructuredBusyError(str(exc)) from exc
        except StructuredSchemaError as exc:
            raise StructuredIntegrityError(str(exc)) from exc
        except (StructuredNamespaceMismatch, StructuredStorageError) as exc:
            raise StructuredConflictError(str(exc)) from exc


def _peer_identities(bundle: StructuredRegistryBundle) -> tuple[SecurityIdentity, ...]:
    values: list[SecurityIdentity] = []
    for peer_set in bundle.peer_sets.peer_sets:
        for item in peer_set.companies:
            market = "SSE" if item.canonical_ticker.endswith(".SH") else "SZSE"
            values.append(
                SecurityIdentity(
                    company_id=f"company:{item.supplier_security_code}",
                    security_id=f"security:{item.canonical_ticker}",
                    canonical_ticker=item.canonical_ticker,
                    security_code=item.supplier_security_code,
                    market=market,
                    current_name=item.company_name,
                    security_type="A_SHARE",
                    source_record_ids=(f"peer-set:{peer_set.peer_set_id}",),
                )
            )
    return _merge_identities(values)


def _merge_identities(
    *groups: Iterable[SecurityIdentity],
) -> tuple[SecurityIdentity, ...]:
    values: dict[str, SecurityIdentity] = {}
    for item in (identity for group in groups for identity in group):
        existing = values.get(item.security_id)
        if existing is not None and existing != item:
            raise ValueError(
                f"conflicting structured security identity: {item.security_id}"
            )
        values[item.security_id] = item
    return tuple(sorted(values.values(), key=lambda item: item.security_id))


def _resolution_mapping(value: Any) -> dict[str, Any]:
    return {
        "query": value.query,
        "status": value.status.value,
        "identity": (
            None if value.identity is None else _identity_mapping(value.identity)
        ),
        "candidates": [_identity_mapping(item) for item in value.candidates],
        "prerequisite_dataset_ids": list(value.prerequisite_dataset_ids),
        "reason_codes": list(value.reason_codes),
        "performed_io": bool(value.performed_io),
    }


def _identity_mapping(value: SecurityIdentity) -> dict[str, Any]:
    raw = asdict(value)
    for name in ("listing_date", "delisting_date"):
        if raw[name] is not None:
            raw[name] = raw[name].isoformat()
    raw["aliases"] = [
        {
            "name": item["name"],
            "valid_from": (
                None
                if item["valid_from"] is None
                else item["valid_from"].isoformat()
            ),
            "valid_to": (
                None if item["valid_to"] is None else item["valid_to"].isoformat()
            ),
        }
        for item in raw["aliases"]
    ]
    raw["source_record_ids"] = list(raw["source_record_ids"])
    return raw


def _as_date(value: date | datetime | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return value.date() if isinstance(value, datetime) else value


def _as_datetime(
    value: date | datetime | None,
    *,
    fallback: datetime,
) -> datetime:
    if value is None:
        value = fallback
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("structured as_of must be timezone-aware")
        return value.astimezone(timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _page(
    values: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    if limit < 1 or offset < 0:
        raise ValueError("pagination requires positive limit and non-negative offset")
    items = list(values[offset : offset + limit])
    next_offset = offset + len(items)
    return {
        "total": len(values),
        "items": items,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset if next_offset < len(values) else None,
    }


__all__ = ["StructuredDataService"]
