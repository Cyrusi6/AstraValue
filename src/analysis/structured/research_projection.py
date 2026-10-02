"""Scoped, read-only source records accompanying the formal fact projection."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from functools import lru_cache
from typing import Any

from .mappings import lifecycle_state, map_counterparty_record, record_dates, record_dimensions
from .materialization import _enum, _get, _source_for_snapshot, _timestamp
from .scope import field_selected, load_research_profile, load_scope


RESEARCH_PROJECTION_VERSION = "structured-research-projection-v1.0.0"


def build_research_projection(storage, repository, run_id: str, *,
                              as_of: datetime | None = None,
                              strict_historical: bool = False,
                              research_profile_id: str | None = None) -> dict[str, Any]:
    """Retain source text and collection outcomes without granting numeric admission.

    Read only committed, validated pages through the same storage boundary used
    by the materializer. Raw values remain supplier observations; only selected
    formal facts can participate in calculations.
    """
    cutoff = _timestamp(as_of) if as_of is not None else None
    if strict_historical and cutoff is None:
        raise ValueError("strict_historical requires timezone-aware as_of")
    context = storage.get_run_context(run_id)
    if not any(_enum(_get(event, "event_type")) == "finalized"
               for event in repository.list_run_events(run_id)):
        raise ValueError("research projection requires a finalized structured run")
    profile_id = research_profile_id or context.frozen_config.get("research_profile_id")
    scope = load_scope()
    profile = load_research_profile(profile_id) if profile_id else None
    selected_datasets = set(profile["datasets"] if profile else (
        name for name, rule in scope["datasets"].items() if rule["selection"] != "excluded"))
    jobs = {job["job_id"]: job for job in storage.list_jobs(run_id, limit=None)}
    datasets = {value["dataset_id"] for value in context.frozen_config.get("datasets", ())}
    definitions = {(value["source_definition_id"], str(value.get("version", value.get("source_definition_version")))): value
                   for value in context.frozen_config.get("source_definitions", ())}
    records, gaps, sources = [], [], {}

    @lru_cache(maxsize=128)
    def snapshot_for(snapshot_id):
        return repository.get_raw_resource_snapshot(snapshot_id)

    def gap(record, reason):
        gaps.append({"record_version_id": record.get("record_version_id"),
                     "snapshot_id": record.get("snapshot_id"), "job_id": record.get("job_id"),
                     "reason": reason})

    for record, _fields in storage.iter_committed_record_bundles(run_id, dataset_ids=sorted(selected_datasets)):
        job = jobs.get(record.get("job_id"))
        if not job or job["dataset_id"] not in datasets:
            gap(record, "job_not_in_frozen_context")
            continue
        if job.get("purpose") in {"company_type", "report_catalog"}:
            continue
        if record.get("_committed_attempt_outcome") != "success":
            gap(record, "page_attempt_not_successful")
            continue
        definition = definitions.get((job["source_definition_id"], str(job["source_definition_version"])))
        if definition is None:
            gap(record, "source_not_frozen")
            continue
        try:
            observed = _timestamp(record.get("observed_at"))
            original_available = _timestamp(record.get("available_at"))
            snapshot = snapshot_for(record["snapshot_id"])
            available = max(observed, original_available, _timestamp(snapshot.available_at))
            _timestamp(snapshot.created_at)
        except (ValueError, KeyError, LookupError, RuntimeError):
            gap(record, "timestamp_or_snapshot_missing")
            continue
        if (snapshot.storage_namespace_id != context.storage_namespace_id
                or snapshot.source_definition_id != job["source_definition_id"]
                or str(snapshot.source_definition_version) != str(job["source_definition_version"])
                or snapshot.policy_decision != "allowed" or snapshot.snapshot_id != record["snapshot_id"]
                or len(snapshot.sha256) != 64 or any(c not in "0123456789abcdef" for c in snapshot.sha256)):
            gap(record, "snapshot_binding_or_policy_invalid")
            continue
        if snapshot.physical_query_plan_item_id != job["plan_item_id"]:
            try:
                storage.snapshot_observation_id(job["job_id"], snapshot.snapshot_id)
            except (ValueError, LookupError, RuntimeError, AttributeError):
                gap(record, "snapshot_binding_or_policy_invalid")
                continue
        if strict_historical and available > cutoff:
            gap(record, "not_available_at_cutoff")
            continue
        dataset = job["dataset_id"]
        row = record.get("raw_row") or {}
        fields = {name: value for name, value in row.items() if field_selected(dataset, name, profile_id)}
        if not fields:
            continue
        source = _source_for_snapshot(snapshot, job, definition)
        sources[source.source_id] = source
        period = next((str(row[name])[:10] for name in ("REPORT_DATE", "END_DATE", "TRADE_DATE", "statDate", "date")
                       if row.get(name)), None)
        value = {"dataset_id": dataset, "company": context.ticker, "run_id": run_id,
                 "record_id": record["record_version_id"], "snapshot_id": snapshot.snapshot_id,
                 "snapshot_sha256": snapshot.sha256, "storage_namespace_id": context.storage_namespace_id,
                 "row_key": record["row_key"], "period": period, "available_at": available.isoformat(),
                 "original_available_at": original_available.isoformat(), "observed_at": observed.isoformat(),
                 "supersedes_record_version_id": record.get("supersedes_record_version_id"),
                 "source_definition_id": job["source_definition_id"],
                 "source_definition_version": job["source_definition_version"],
                 "source_ids": [source.source_id], "dimensions": dict(record_dimensions(dataset, row)),
                 "dates": dict(record_dates(dataset, row)), "lifecycle": asdict(lifecycle_state(dataset, row)),
                 "fields": fields, "numeric_consumption": "only_selected_fact_ids",
                 "text_consumption": "supplier_statement_not_research_conclusion"}
        if dataset == "customers_peer":
            value["counterparty"] = asdict(map_counterparty_record(row))
        records.append(value)
    coverage = [row for row in storage.list_acquisition_coverage(run_id=run_id, limit=None)
                if row.get("dataset_id") in selected_datasets]
    return {"version": RESEARCH_PROJECTION_VERSION, "scope_sha256": scope["content_sha256"],
            "profile_id": profile_id, "profile_sha256": profile["content_sha256"] if profile else None,
            "records": sorted(records, key=lambda row: row["record_id"]),
            "sources": [sources[key] for key in sorted(sources)], "acquisition_coverage": coverage,
            "gaps": gaps}
