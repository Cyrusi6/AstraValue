"""Bounded CNINFO catalog and selected-body jobs on the acquisition runtime."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
import time as clock
from uuid import uuid4
from zoneinfo import ZoneInfo

from analysis.acquisition.adapters.factory import AcquisitionAdapterFactory
from analysis.acquisition.adapters.official import CninfoAcquisitionAdapter
from analysis.acquisition.content import extract_announcement_text
from analysis.acquisition.dependencies import order_dependency_plans
from analysis.acquisition.models import (AcquisitionMode, AcquisitionPlan, CoveragePlanDisposition,
                                         PhysicalQueryCoverageLink, stable_acquisition_id)
from analysis.acquisition.orchestrator import AcquisitionOrchestrator
from analysis.acquisition.planner import _window_slices
from analysis.acquisition.retained_inventory import export_cninfo_inventory, inventory_batch, plan_retained_inventory
from analysis.acquisition.runtime import AcquisitionRuntime


class ReportCheckpoint(BaseException):
    """Cooperative stop; acquisition releases its lease without finalizing a page."""


class _BoundedTransport:
    def __init__(self, transport, deadline: float | None, requests: list[dict]):
        self.transport, self.deadline, self.requests = transport, deadline, requests

    def request(self, work):
        if self.deadline is not None:
            if clock.monotonic() >= self.deadline:
                raise ReportCheckpoint("report_materials_time_bound")
            work = replace(work, context={**work.context, "deadline_monotonic": min(
                float(work.context.get("deadline_monotonic", self.deadline)), self.deadline)})
        if work.page > 20:
            raise ValueError("report_catalog_page_bound_exceeded")
        try:
            result = self.transport.request(work)
        except Exception as exc:
            self.requests.append({"url": work.url, "page": work.page, "status": "failed",
                                  "reason": type(exc).__name__, "cache_reused": False})
            raise
        self.requests.append({"url": work.url, "page": work.page, "http_status": result.status_code,
                              "sha256": result.sha256, "cache_reused": False})
        return result


class _CatalogAdapter(CninfoAcquisitionAdapter):
    def _parse(self, body, work, *, replayable):
        parsed = super()._parse(body, work, replayable=replayable)
        # Retain every directory row and its proof. This first phase requests no
        # attachment; selected rows enter plan_retained_inventory separately.
        return replace(parsed, resources=tuple(replace(row, required_fetch=False)
                                              for row in parsed.resources))


class _ReportAdapterFactory(AcquisitionAdapterFactory):
    def __init__(self, deadline, requests):
        super().__init__()
        self.deadline, self.requests = deadline, requests

    def create(self, source_definition, *, transport, snapshot_reader):
        if source_definition.source_definition_id != "cninfo.disclosures":
            raise ValueError("report_materials_source_not_selected")
        # Keep the normal registry policy check before wrapping the adapter.
        super().create(source_definition, transport=transport, snapshot_reader=snapshot_reader)
        return _CatalogAdapter(_BoundedTransport(transport, self.deadline, self.requests), snapshot_reader)


_CATALOG_QUERIES = {"cninfo.company_bootstrap", "cninfo.periodic_report"}


def _catalog_retry_plan(runtime, parent_id: str):
    """Keep frozen windows/contracts and record each plan's predecessor."""
    repo = runtime.repository
    parent = repo.get_run(parent_id)
    run = parent.model_copy(update={"run_id": str(uuid4()), "parent_run_id": parent_id,
        "mode": AcquisitionMode.RECONCILE, "as_of": runtime.clock(), "created_at": runtime.clock(),
        "reconcile_target": {"kind": "report_catalog_resume", "parent_run_id": parent_id}})
    plans = repo.list_physical_query_plan_items(parent_id)
    if parent.run_kind.value != "ad_hoc" or any(p.query_id not in _CATALOG_QUERIES for p in plans):
        raise ValueError("report_catalog_parent_scope_invalid")
    plan_ids = {p.plan_item_id: stable_acquisition_id("report-plan", [run.run_id, p.plan_item_id]) for p in plans}
    run = run.model_copy(update={"reconcile_target": {**run.reconcile_target,
        "plan_predecessors": {current: prior for prior, current in plan_ids.items()}}})
    coverage = repo.list_coverage_entries(parent_id)
    coverage_ids = {c.coverage_entry_id: stable_acquisition_id("report-coverage", [run.run_id, c.coverage_entry_id])
                    for c in coverage}
    return AcquisitionPlan(run=run, physical_query_plan_items=tuple(p.model_copy(update={
        "run_id": run.run_id, "plan_item_id": plan_ids[p.plan_item_id],
        "prerequisite_plan_item_ids": tuple(plan_ids[key] for key in p.prerequisite_plan_item_ids)}) for p in plans),
        coverage_entries=tuple(c.model_copy(update={"run_id": run.run_id,
            "coverage_entry_id": coverage_ids[c.coverage_entry_id]}) for c in coverage),
        coverage_links=tuple(PhysicalQueryCoverageLink(plan_item_id=plan_ids[link.plan_item_id],
            coverage_entry_id=coverage_ids[link.coverage_entry_id])
            for link in repo.list_physical_query_coverage_links(run_id=parent_id)))


class _CatalogEvidenceView:
    """Read original page evidence through explicit report recovery links.

    This view only extends plan-scoped reads inside the report orchestrator.
    Writes, run-scoped reads and all stored identities remain on the real
    repository. Reused pages create no HTTP observation, attempt or snapshot.
    """
    def __init__(self, runtime):
        self.runtime, self.raw = runtime, runtime.repository
        self.ancestors: dict[str, list] = {}
        self.proof_filter: dict[str, set[str]] = {}

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def prepare(self, plan):
        chain, current = [], plan
        excluded = {"run_id", "plan_item_id", "prerequisite_plan_item_ids"}
        while True:
            run = self.raw.get_run(current.run_id)
            target = run.reconcile_target or {}
            if target.get("kind") != "report_catalog_resume":
                break
            parent_id = target.get("plan_predecessors", {}).get(current.plan_item_id)
            if not parent_id:
                raise ValueError("report_catalog_parent_scope_invalid")
            parent = self.raw.get_physical_query_plan_item(parent_id)
            prior = self.raw.get_run(parent.run_id)
            if (parent.plan_item_id in {p.plan_item_id for p in chain}
                    or run.parent_run_id != prior.run_id
                    or (run.reconcile_target or {}).get("kind") != "report_catalog_resume"
                    or run.ticker != prior.ticker or run.storage_namespace_id != prior.storage_namespace_id
                    or run.storage_namespace_id != self.runtime.namespace_id
                    or run.source_definition_refs != prior.source_definition_refs
                    or current.model_dump(exclude=excluded) != parent.model_dump(exclude=excluded)):
                raise ValueError("report_catalog_parent_scope_invalid")
            chain.append(parent)
            current = parent
        self.ancestors[plan.plan_item_id] = list(reversed(chain))
        attempts = self.list_attempts(plan_item_id=plan.plan_item_id, limit=None)
        latest = {}
        for attempt in attempts:
            for proof in self.raw.list_discovery_proofs(attempt.attempt_id):
                latest[proof.page_number or 1] = proof
        prefix, ids, total, count = [], set(), None, 0
        for page in range(1, len(latest) + 1):
            proof = latest.get(page)
            if proof is None:
                break
            observation = self.raw.get_discovery_observation(proof.observation_id)
            snapshot = self.raw.get_raw_resource_snapshot(proof.discovery_snapshot_id)
            resources = self.raw.list_discovered_resources(proof.observation_id)
            if (proof.proof_kind != "http_response" or proof.http_status != 200 or not proof.schema_valid
                    or not proof.body_retained or not proof.replayable
                    or proof.attempt_id != observation.attempt_id
                    or proof.physical_query_plan_item_id != observation.physical_query_plan_item_id
                    or proof.discovery_snapshot_id != observation.snapshot_id
                    or snapshot.storage_namespace_id != self.runtime.namespace_id
                    or snapshot.physical_query_plan_item_id != proof.physical_query_plan_item_id
                    or snapshot.page_number != proof.page_number
                    or snapshot.sha256 != proof.response_sha256 or snapshot.sha256 != observation.response_sha256
                    or snapshot.byte_length != proof.response_byte_length
                    or snapshot.byte_length != observation.response_byte_length
                    or len(resources) != proof.normalized_row_count
                    or any(r.required_fetch or r.proof_id != proof.proof_id for r in resources)):
                raise ValueError("report_catalog_prefix_lineage_invalid")
            integrity = self.raw.list_snapshot_integrity_events(snapshot.snapshot_id)
            if any(e.status.value == "quarantined" for e in integrity):
                raise ValueError("report_catalog_prefix_quarantined")
            try:
                self.runtime.snapshot_bytes(snapshot.snapshot_id)
            except Exception as exc:
                raise ValueError("report_catalog_prefix_hash_invalid") from exc
            row_ids = {r.canonical_resource_id for r in resources}
            if (len(row_ids) != len(resources) or ids.intersection(row_ids)
                    or total is not None and proof.declared_total != total):
                break
            total = proof.declared_total
            count += proof.normalized_row_count
            if (total is None or count > total or proof.terminal and count != total
                    or proof.declared_page_count is not None and proof.terminal
                    and proof.declared_page_count not in {page, 0 if count == 0 else page}):
                break
            prefix.append(proof)
            ids.update(row_ids)
            if proof.terminal:
                break
        selected = {p.proof_id for p in prefix}
        # Existing pages outside the validated prefix stay in the audit DB but
        # cannot satisfy completion. Newly requested proofs remain visible.
        for attempt in attempts:
            self.proof_filter[attempt.attempt_id] = selected
        return prefix

    def list_attempts(self, *, plan_item_id=None, **kwargs):
        if plan_item_id not in self.ancestors:
            return self.raw.list_attempts(plan_item_id=plan_item_id, **kwargs)
        limit, offset = kwargs.pop("limit", 500), kwargs.pop("offset", 0)
        attempts = [attempt for plan in self.ancestors[plan_item_id]
                    for attempt in self.raw.list_attempts(plan_item_id=plan.plan_item_id, limit=None, **kwargs)]
        attempts.extend(self.raw.list_attempts(plan_item_id=plan_item_id, limit=None, **kwargs))
        return attempts[offset:] if limit is None else attempts[offset:offset + limit]

    def list_discovery_proofs(self, attempt_id):
        allowed = self.proof_filter.get(attempt_id)
        return [p for p in self.raw.list_discovery_proofs(attempt_id) if allowed is None or p.proof_id in allowed]


class _ReportOrchestrator(AcquisitionOrchestrator):
    def __init__(self, runtime):
        super().__init__(runtime, now=runtime.clock, monotonic=runtime.monotonic_clock, sleep=runtime.sleeper)
        self.repository = _CatalogEvidenceView(runtime)
        self._catalog_next_page = None

    def _execute_discovery_plan(self, run, plan_item, **kwargs):
        if plan_item.query_id not in _CATALOG_QUERIES or plan_item.retained_inventory_ref is not None:
            return super()._execute_discovery_plan(run, plan_item, **kwargs)
        prefix = self.repository.prepare(plan_item)
        self._catalog_next_page = len(prefix) + 1
        try:
            return super()._execute_discovery_plan(run, plan_item, **kwargs)
        finally:
            self._catalog_next_page = None

    def _resume_position(self, query, attempt):
        if self._catalog_next_page is not None:
            return self._catalog_next_page, None
        return super()._resume_position(query, attempt)


def _catalog_plan(runtime, ticker: str, cutoff: date):
    definition = runtime.loaded_registry.definition("cninfo.disclosures")
    profile = runtime.build_profile(ticker)
    now = runtime.clock()
    start = datetime(cutoff.year - 1, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
    end = min(datetime.combine(cutoff + timedelta(days=1), time.min,
                               ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc), now)
    if end <= start:
        raise ValueError("report_catalog_window_invalid")
    base = runtime.create_plan(profile, mode="incremental", run_kind="ad_hoc",
                               as_of=now, start_at=start, persist=False)
    plans, coverage, links = [], [], []
    for query in definition.queries:
        if query.query_id not in _CATALOG_QUERIES:
            continue
        topic = next(t for t in runtime.loaded_questions.question_set.topics if t.question_id in query.question_ids)
        for lower, upper in _window_slices(start, end, query.max_window_days):
            key = runtime.planner._physical_execution_key(profile, definition, query, lower, upper)
            item = runtime.planner._physical_plan_item(base.run.run_id, profile, definition, query,
                                                       lower, upper, key, len(plans))
            entry = runtime.planner._coverage_entry(base.run.run_id, definition, topic, query.query_id,
                                                     lower, upper, CoveragePlanDisposition.REQUIRED, None)
            plans.append(item)
            coverage.append(entry)
            links.append(PhysicalQueryCoverageLink(plan_item_id=item.plan_item_id,
                                                   coverage_entry_id=entry.coverage_entry_id))
    run = base.run.model_copy(update={"source_definition_refs": tuple(
        ref for ref in base.run.source_definition_refs if ref.source_definition_id == definition.source_definition_id)})
    return AcquisitionPlan(run=run, physical_query_plan_items=order_dependency_plans(tuple(plans), (definition,)),
                           coverage_entries=tuple(coverage), coverage_links=tuple(links))


class ReportAcquisition:
    """One resumable catalog run and independent, one-resource body runs."""
    def __init__(self, *, db_path: Path, data_root: Path, deadline: float | None):
        self.requests: list[dict[str, Any]] = []
        self.runtime = AcquisitionRuntime.create(db_path, data_root,
            adapter_factory=_ReportAdapterFactory(deadline, self.requests), orchestrator_factory=_ReportOrchestrator)

    def close(self):
        self.runtime.close()

    def _run(self, state: dict, create: Callable, save: Callable, retry: Callable | None = None):
        run_id = state.get("run_id")
        failed_run_id = None
        if run_id:
            events = self.runtime.repository.list_run_events(run_id)
            final = next((e for e in reversed(events) if e.event_type.value == "finalized"), None)
            if final is not None and final.material_gap_count:
                failed_run_id = run_id
                run_id = None
        if run_id is None:
            plan = retry(failed_run_id) if failed_run_id and retry else create()
            # Save the run identity before it can perform network I/O.
            self.runtime.orchestrator.persist_plan(plan)
            if failed_run_id:
                state.setdefault("previous_run_ids", []).append(failed_run_id)
            run_id = state["run_id"] = plan.run.run_id
            save()
        result = self.runtime.orchestrator.execute_run(run_id)
        state.update(query_complete=result.material_gap_count == 0,
                     material_gap_count=result.material_gap_count,
                     outcome_counts=result.outcome_counts)
        save()
        return result

    def catalog(self, *, ticker: str, cutoff: date, state: dict, save: Callable):
        result = self._run(state, lambda: _catalog_plan(self.runtime, ticker, cutoff), save,
                           retry=lambda parent: _catalog_retry_plan(self.runtime, parent))
        if result.material_gap_count:
            return {"query_complete": False, "entries": [], "reason": "report_catalog_incomplete"}
        resources, proof_ids, run_ids = [], set(), {state["run_id"]}
        repo = self.runtime.orchestrator.repository
        for plan in self.runtime.repository.list_physical_query_plan_items(state["run_id"]):
            for proof in repo.prepare(plan):
                proof_ids.add(proof.proof_id)
                run_ids.add(self.runtime.repository.get_attempt(proof.attempt_id).run_id)
                resources.extend(r for r in repo.list_discovered_resources(proof.observation_id)
                                 if r.canonical_resource_id.startswith("cninfo:"))
        if not resources:
            return {"query_complete": True, "entries": [], "reason": "catalog_no_data"}
        bundle = export_cninfo_inventory(self.runtime.db_path, self.runtime.data_root,
                                         sorted(run_ids), ticker=ticker)
        bundle = inventory_batch(bundle, [r for r in bundle["entries"] if r["proof_id"] in proof_ids])
        return {"query_complete": True, "entries": bundle["entries"], "bundle": bundle}

    def body(self, *, bundle: dict, entry: dict, state: dict, save: Callable):
        result = self._run(state, lambda: plan_retained_inventory(self.runtime,
                           inventory_batch(bundle, [entry]), persist=False), save)
        if result.material_gap_count:
            reasons = []
            for attempt in self.runtime.repository.list_attempts(run_id=state["run_id"], limit=None):
                reasons.extend(e.reason_code for e in self.runtime.repository.list_attempt_events(attempt.attempt_id)
                               if e.event_type.value == "outcome_terminal" and e.reason_code)
            raise ValueError("report_body_failed:" + ",".join(sorted(set(reasons))))
        snapshot = self.runtime.repository.find_raw_resource_snapshot(resource_role="content",
            source_definition_id="cninfo.disclosures", canonical_resource_id=entry["canonical_resource_id"])
        if snapshot is None:
            raise ValueError("report_body_snapshot_missing")
        self.runtime.snapshot_bytes(snapshot.snapshot_id)
        document = {"snapshot_id": snapshot.snapshot_id, "original_sha256": snapshot.sha256,
                "original_path": str((self.runtime.data_root / snapshot.archive_relative_path).resolve()),
                "mime_type": snapshot.mime_type,
                "available_at": snapshot.available_at.isoformat(), "available_at_basis": snapshot.available_at_basis.value,
                "source_definition_id": snapshot.source_definition_id,
                "source_definition_version": snapshot.source_definition_version,
                "download_status": "downloaded", "body_run_id": state["run_id"]}
        try:
            extraction = extract_announcement_text(self.runtime, snapshot.snapshot_id)
            document.update(derived_artifact_id=extraction.derived_artifact_id, text_derivation_status="parsed")
        except Exception as exc:
            # A valid downloaded snapshot survives an OCR/parser gap. The
            # located parser records its own independent status in the service.
            document.update(text_derivation_status="parse_pending", text_derivation_error=str(exc))
        return document
