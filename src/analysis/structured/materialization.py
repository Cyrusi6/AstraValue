from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from functools import lru_cache
from typing import Any, Mapping

from analysis.models import (
    DimensionalFactRecord, EventRecord, FactRecord, SourceRecord,
    StructuredAcquisitionMethod, StructuredFactAdmission, StructuredFactNature,
    StructuredQualityStatus, VerificationStatus,
)
from .materialization_contracts import LEGACY_CONTRACTS
from .records import NormalizedValue, PeriodKind, ValueKind, derive_single_quarter, derive_ttm
from .registry import canonical_registry_sha256
from .storage import StructuredStorage, canonical_json, canonical_sha256

MATERIALIZATION_VERSION = "structured-materialization-v2.0.0"
PERIOD_FORMULA_VERSION = "structured-periods-v1.0.0"


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    run_id: str
    facts: tuple[FactRecord, ...]
    dimensional_facts: tuple[DimensionalFactRecord, ...]
    events: tuple[EventRecord, ...]
    sources: tuple[SourceRecord, ...]
    candidates: int
    skipped: int
    gaps: tuple[str, ...]
    field_gaps: tuple[dict, ...]
    selected_fact_ids: tuple[str, ...]
    selected_dimensional_fact_ids: tuple[str, ...]
    materialization_hash: str
    as_of: str | None
    strict_historical: bool
    contract_hash: str
    interpretation_contract: dict | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "materialization_version": MATERIALIZATION_VERSION,
            "materialization_hash": self.materialization_hash,
            "fact_count": len(self.facts), "dimensional_fact_count": len(self.dimensional_facts),
            "event_count": len(self.events), "source_count": len(self.sources),
            "candidate_count": self.candidates, "skipped_count": self.skipped,
            "gaps": list(self.gaps), "gap_counts": dict(sorted(Counter(
                g["reason"] for g in self.field_gaps).items())),
            "selected_fact_ids": list(self.selected_fact_ids),
            "selected_dimensional_fact_ids": list(self.selected_dimensional_fact_ids),
            "as_of": self.as_of, "strict_historical": self.strict_historical,
            "contract_hash": self.contract_hash, "performed_network_io": False,
            **({"interpretation_contract": self.interpretation_contract,
                "semantic_gap_counts": dict(sorted(Counter(
                    reason for f in self.facts for reason in f.metadata.get("semantic_gaps", [])).items()))}
               if self.interpretation_contract is not None else {}),
        }


class StructuredFactMaterializer:
    """Append-only evidence projection with a separate, cutoff-specific selection.

    Raw SQL input is streamed. Memory is proportional to output evidence and gaps,
    not to every supplier raw field payload. No acquisition or snapshot mutation.
    """

    def __init__(self, storage: StructuredStorage, repository: Any) -> None:
        self.storage, self.repository = storage, repository

    def materialize(self, run_id: str, *, as_of: datetime | None = None,
                    strict_historical: bool = False,
                    interpretation_contract: str | None = None,
                    research_scope: bool | None = None,
                    research_profile_id: str | None = None) -> MaterializationResult:
        cutoff = _timestamp(as_of) if as_of is not None else None
        if strict_historical and cutoff is None:
            raise ValueError("strict_historical requires timezone-aware as_of")
        context = self.storage.get_run_context(run_id)
        if not any(_enum(_get(e, "event_type")) == "finalized"
                   for e in self.repository.list_run_events(run_id)):
            raise ValueError("materialization requires a finalized structured run")
        contract = _contract(context)
        interpretation = None
        if interpretation_contract is not None:
            from .interpretation import load_interpretation
            interpretation = load_interpretation(interpretation_contract, context)
            contract = {"base_contract_hash": canonical_sha256(contract),
                "interpretation": interpretation,
                "rules": {**contract["rules"], **interpretation["rules"]},
                "rejections": {**contract.get("rejections", {}), **interpretation["rejections"]}}
        contract_hash = canonical_sha256(contract)
        if research_scope is None:
            research_scope = bool(context.frozen_config.get("research_scope"))
        research_profile_id = research_profile_id or context.frozen_config.get(
            "research_profile_id"
        )
        if research_profile_id and not research_scope:
            raise ValueError("research_profile_requires_research_scope")
        if research_scope:
            from .scope import load_scope, load_research_profile, field_selected
            scope_contract = {"scope": load_scope()["content_sha256"]}
            if research_profile_id:
                scope_contract["profile"] = load_research_profile(research_profile_id)[
                    "content_sha256"
                ]
            contract_hash = canonical_sha256({"contract": contract_hash, **scope_contract})
        market_start = context.frozen_config.get("valuation_start")
        market_cutoff = (cutoff or self.repository.get_run(run_id).as_of).date().isoformat() if research_scope else None
        market_latest = self.storage.latest_market_date(run_id, market_cutoff) if research_scope else None
        jobs = {str(j["job_id"]): j for j in self.storage.list_jobs(run_id, limit=None)}
        datasets = {d["dataset_id"]: d for d in context.frozen_config.get("datasets", [])}
        frozen_sources = {(s["source_definition_id"], str(s.get("version", s.get("source_definition_version")))): s
                          for s in context.frozen_config.get("source_definitions", [])}
        facts, dimensions, events, field_gaps = [], [], [], []
        sources, gaps = {}, set()
        candidates = skipped = 0

        @lru_cache(maxsize=128)
        def snapshot_for(snapshot_id):
            return self.repository.get_raw_resource_snapshot(snapshot_id)

        def gap(reason, record, field=None, **extra):
            nonlocal skipped
            skipped += 1
            if interpretation is not None and field:
                extra.update(dataset_id=field.get("dataset_id"), original_value=field.get("value"),
                    stored_unit=field.get("unit"), original_quality=field.get("quality"),
                    original_definition_version=field.get("definition_version"))
            field_gaps.append({"record_version_id": record.get("record_version_id"),
                "job_id": record.get("job_id"), "snapshot_id": record.get("snapshot_id"),
                "row_key": record.get("row_key"),
                "field_value_id": (field or {}).get("field_value_id"),
                "field_path": (field or {}).get("field_path"), "reason": reason, **extra})
            gaps.add(reason)

        read_options = {}
        if research_scope:
            mapped_datasets = {rule["dataset_id"] for rule in contract["rules"].values()}
            if research_profile_id:
                profile_datasets = load_research_profile(research_profile_id)["datasets"]
                read_options["dataset_ids"] = [
                    name for name in profile_datasets if name in mapped_datasets
                ]
            else:
                read_options["dataset_ids"] = [
                    name
                    for name, value in load_scope()["datasets"].items()
                    if value["selection"] != "excluded" and name in mapped_datasets
                ]
        for record, fields in self.storage.iter_committed_record_bundles(run_id, **read_options):
            job = jobs.get(str(record.get("job_id")))
            candidates += len(fields)
            if job is None or job["dataset_id"] not in datasets:
                gap("job_not_in_frozen_context", record)
                continue
            if record.get("_committed_attempt_outcome") != "success":
                gap("page_attempt_not_successful", record)
                continue
            dataset_id = job["dataset_id"]
            if research_scope and dataset_id == "market_cap":
                market_date = str(record.get("raw_row", {}).get("TRADE_DATE", ""))[:10]
                if market_date>market_cutoff or (market_start and market_date < market_start) or (not market_start and market_date != market_latest):
                    gap("outside_valuation_window", record)
                    continue
            dataset = datasets[dataset_id]
            source_definition = frozen_sources.get((job["source_definition_id"], str(job["source_definition_version"])))
            if source_definition is None:
                gap("source_not_frozen", record)
                continue
            try:
                available = _timestamp(record.get("available_at"))
                observed = _timestamp(record.get("observed_at"))
                snapshot = snapshot_for(str(record["snapshot_id"]))
                snapshot_available = _timestamp(snapshot.available_at)
                _timestamp(snapshot.created_at)
            except (ValueError, KeyError, LookupError, RuntimeError) as exc:
                gap("timestamp_or_snapshot_missing", record, detail=str(exc))
                continue
            if (snapshot.storage_namespace_id != context.storage_namespace_id
                    or snapshot.source_definition_id != job["source_definition_id"]
                    or str(snapshot.source_definition_version) != str(job["source_definition_version"])
                    or snapshot.policy_decision != "allowed"
                    or snapshot.snapshot_id != record["snapshot_id"]
                    or len(snapshot.sha256) != 64
                    or any(c not in "0123456789abcdef" for c in snapshot.sha256)):
                gap("snapshot_binding_or_policy_invalid", record)
                continue
            if snapshot.physical_query_plan_item_id != job["plan_item_id"]:
                try:
                    self.storage.snapshot_observation_id(job["job_id"], snapshot.snapshot_id)
                except (ValueError, LookupError, RuntimeError, AttributeError):
                    gap("snapshot_binding_or_policy_invalid", record)
                    continue
            # The local observation is proof of availability, never backdate it to
            # a vendor publication string attached to today's revised history.
            effective_available = max(available, observed, snapshot_available)
            if cutoff is not None and strict_historical and effective_available > cutoff:
                gap("not_available_at_cutoff", record)
                continue
            row = record.get("raw_row") or {}
            source = _source_for_snapshot(snapshot, job, source_definition)
            for field in fields:
                raw_name = field.get("raw_field_name")
                if research_scope and not field_selected(
                    dataset_id, raw_name, research_profile_id
                ):
                    gap("outside_research_scope", record, field)
                    continue
                rule = contract["rules"].get(f"{dataset_id}.{raw_name}")
                interpreted = rule is not None and "input_descriptors" in rule
                fact_available = effective_available
                if interpreted:
                    from .interpretation import interpretation_field_failure, interpreted_period_end
                    if strict_historical and _timestamp(interpretation["effective_at"]) > cutoff:
                        gap("interpretation_not_effective_at_cutoff", record, field)
                        continue
                    fact_available = max(effective_available, _timestamp(interpretation["effective_at"]))
                    reason = interpretation_field_failure(context, job, record, field, rule, row, source_definition, interpretation)
                else:
                    reason = _field_failure(context, job, record, field, rule, row, contract.get("rejections", {}))
                if reason:
                    gap(reason, record, field)
                    continue
                try:
                    numeric = _number(field.get("value"))
                    converted = numeric * Decimal(rule["multiplier"])
                    if not math.isfinite(float(converted)):
                        raise ValueError("float overflow")
                except (ValueError, DecimalException, OverflowError):
                    gap("value_missing_or_non_numeric", record, field)
                    continue
                if interpreted:
                    from .interpretation import numeric_semantic_failure
                    reason = numeric_semantic_failure(rule, row, numeric)
                    if reason:
                        gap(reason, record, field)
                        continue
                period_end = _period_end(field.get("period_key"), row, dataset)
                if interpreted:
                    period_end = interpreted_period_end(rule, row, period_end, observed)
                if period_end is None:
                    gap("period_missing_or_conflicting", record, field)
                    continue
                period_kind, value_kind = rule["period_kind"], rule["value_kind"]
                if row.get("CURRENCY") not in (None, "", "CNY"):
                    gap("currency_not_supported_by_rule", record, field)
                    continue
                if rule["nature"] != "announced_plan" and period_end > effective_available.date():
                    gap("period_after_observation", record, field)
                    continue
                if value_kind == "flow" and period_kind in {"cumulative", "annual", "single_quarter"}:
                    if (period_end.month, period_end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
                        gap("non_calendar_quarter", record, field)
                        continue
                metadata = _lineage(context, job, record, field, rule, snapshot, contract_hash)
                metadata.update(value_kind=value_kind, decimal_value=format(converted, "f"),
                    available_at=fact_available.isoformat(),
                    original_available_at=available.isoformat(),
                    point_in_time_basis="max_record_observation_snapshot_availability",
                    historical_publication_proven=False,
                    requires_materialization_selection=True)
                if interpreted:
                    metadata.update(
                        statement_org_type=row.get("ORG_TYPE"),
                        interpretation_contract_id=interpretation["contract_id"],
                        interpretation_version=interpretation["version"],
                        interpretation_sha256=interpretation["content_sha256"],
                        interpretation_effective_at=interpretation["effective_at"],
                        interpretation_definition_id=rule["definition_id"],
                        interpretation_evidence=rule["evidence"],
                        interpretation_semantics=rule["semantics"],
                        original_field_descriptor={k: field.get(k) for k in (
                            "standard_field_id", "definition_version", "nature", "quality", "unit", "period_key")},
                        original_source_available_at=effective_available.isoformat(),
                        original_pub_date=row.get("pubDate"),
                        original_row_ordinal=record.get("row_ordinal"),
                        original_source_definition_hash=source_definition.get("content_hash"),
                        trading_status=row.get("tradestatus"), is_st=row.get("isST"),
                        period_window_confirmed=rule["period_window_confirmed"],
                        semantic_gaps=[] if rule["period_window_confirmed"] else ["period_window_unconfirmed"],
                        point_in_time_basis="max_original_availability_and_interpretation_effective_at")
                identity = {"version": MATERIALIZATION_VERSION, "contract": contract_hash,
                    "namespace": context.storage_namespace_id, "run": run_id,
                    "job": job["job_id"], "record": record["record_version_id"],
                    "snapshot": snapshot.sha256, "field": field["field_path"],
                    "definition": field["definition_version"]}
                fact_id = _id("structured-fact", identity)
                admission = _admission(job, record, field, rule, snapshot, observed, fact_available,
                    definition_version=interpretation["contract_id"] if interpreted else None)
                if rule["nature"] == "announced_plan":
                    announced = _event_date(row.get("NOTICE_DATE") or row.get("PUBLISH_DATE"))
                    if announced is None or announced > effective_available:
                        gap("event_announcement_time_missing_or_future", record, field)
                        continue
                    events.append(EventRecord(
                        event_id=_id("structured-event", identity), ticker=context.ticker,
                        event_type="dividend", event_subtype="cash_dividend_plan_terms",
                        announced_at=announced, available_at=effective_available,
                        period_end=period_end, lifecycle_state="announced_plan",
                        summary="供应商记录的每股税前现金分红方案；不代表已实施或已支付",
                        event_terms={"cash_dividend_per_share_plan": float(converted),
                                     "unit": rule["unit"], "nature": "announced_plan"},
                        source_ids=[source.source_id], status_updated_at=effective_available,
                        data_snapshot_id=_id("structured-input", {"run": run_id, "contract": contract_hash}),
                        metadata=metadata | {"lifecycle_basis": "registered_plan_field",
                            "announcement_timezone": "Asia/Shanghai",
                            "announcement_date_policy": "date_only_end_of_day",
                            "original_lifecycle": row.get("ASSIGN_PROGRESS")},
                        verification_status=VerificationStatus.PENDING))
                    sources[source.source_id] = source
                    continue
                dim = _dimension(dataset_id, row)
                if dataset_id in {"segments", "float_holders_history", "customers_peer"} and dim is None:
                    gap("dimension_identity_missing", record, field)
                    continue
                sources[source.source_id] = source
                common = dict(ticker=context.ticker, metric_id=rule["standard_field"],
                    value=float(converted), unit=rule["unit"], currency="CNY",
                    period_start=_period_start(period_end, period_kind), period_end=period_end,
                    period_type=period_kind, scope=rule["scope"], source_ids=[source.source_id],
                    structured_admission=admission, verification_status=VerificationStatus.SUPPLIER_DIRECT,
                    metadata=metadata)
                if dim:
                    dimensions.append(DimensionalFactRecord(
                        **common, dimensional_fact_id=fact_id.replace("structured-fact", "structured-dimension"),
                        **dim, available_at=effective_available,
                        data_snapshot_id=_id("structured-input", {"run": run_id, "contract": contract_hash})))
                else:
                    facts.append(FactRecord(**common, fact_id=fact_id, as_of=fact_available,
                        original_label=raw_name, restatement_version=record["record_version_id"],
                        is_restated=bool(record.get("supersedes_record_version_id"))))

        selected, selection_gaps = _select(facts)
        selected_dimensions, dimension_gaps = _select(dimensions)
        gaps.update(selection_gaps | dimension_gaps)
        derived, derivation_gaps = _derive(selected)
        facts.extend(derived)
        selected.extend(derived)
        gaps.update(derivation_gaps)
        ratios, ratio_gaps = _derive_gross_margin(selected)
        facts.extend(ratios)
        selected.extend(ratios)
        gaps.update(ratio_gaps)
        facts.sort(key=lambda x: x.fact_id)
        dimensions.sort(key=lambda x: x.dimensional_fact_id)
        events.sort(key=lambda x: x.event_id)
        field_gaps.sort(key=canonical_json)
        sources_tuple = tuple(sorted(sources.values(), key=lambda x: x.source_id))
        selected_ids = tuple(sorted(f.fact_id for f in selected))
        selected_dim_ids = tuple(sorted(f.dimensional_fact_id for f in selected_dimensions))
        # Incremental digest avoids duplicating the complete projection JSON in RAM.
        digest = hashlib.sha256()
        header = {"run_id": run_id, "version": MATERIALIZATION_VERSION,
                  "contract_hash": contract_hash, "as_of": cutoff.isoformat() if cutoff else None,
                  "strict_historical": strict_historical, "gaps": sorted(gaps),
                  "selected_fact_ids": selected_ids, "selected_dimension_ids": selected_dim_ids,
                  "candidates": candidates, "skipped": skipped}
        for group in ([header], facts, dimensions, events, sources_tuple, field_gaps):
            for item in group:
                payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                digest.update(canonical_json(payload).encode("utf-8") + b"\n")
            digest.update(b"\n")
        return MaterializationResult(run_id, tuple(facts), tuple(dimensions), tuple(events),
            sources_tuple, candidates, skipped, tuple(sorted(gaps)), tuple(field_gaps),
            selected_ids, selected_dim_ids, digest.hexdigest(), header["as_of"],
            strict_historical, contract_hash, interpretation)


def _contract(context):
    # New contexts may carry their complete registry and explicit rule semantics.
    # The registry digest must match the already persisted context, not today's config.
    embedded = context.frozen_config.get("materialization_contract")
    if embedded is not None:
        registry = embedded["field_registry"]
        if (canonical_registry_sha256(registry) != context.field_registry_hash
                or registry["version"] != context.field_registry_version
                or registry["registry_id"] != context.field_registry_id):
            raise ValueError("frozen materialization registry mismatch")
        definitions = {f["field_id"]: f for f in registry["fields"]}
        rules = {}
        for key, rule in embedded["rules"].items():
            definition = definitions.get(key, {})
            if (definition.get("definition_status") == "confirmed"
                    and definition.get("unit_status") == "confirmed"
                    and definition.get("formula_eligible")):
                _validate_rule(key, rule)
                rules[key] = rule
        return {"field_registry_version": context.field_registry_version, "rules": rules}
    contract = LEGACY_CONTRACTS.get(context.field_registry_hash)
    if contract is None or contract["field_registry_version"] != context.field_registry_version or contract["field_registry_id"] != context.field_registry_id:
        raise ValueError("unsupported frozen field registry; explicit versioned materialization contract required")
    return contract


def _validate_rule(key, rule):
    for name in ("dataset_id", "raw_field", "standard_field", "nature", "unit", "stored_unit",
                 "original_unit", "period_kind", "value_kind", "definition_id", "multiplier", "scope"):
        if not rule.get(name):
            raise ValueError(f"incomplete materialization rule: {key}.{name}")
    if key != f"{rule['dataset_id']}.{rule['raw_field']}":
        raise ValueError("materialization rule identity mismatch")
    PeriodKind(rule["period_kind"])
    ValueKind(rule["value_kind"])
    if _number(rule["multiplier"]) <= 0:
        raise ValueError("unit multiplier must be positive")


def _field_failure(context, job, record, field, rule, row, rejections):
    if rule is None:
        if field.get("nature") in {"forecast", "provider_estimate", "platform_label", "source_text"}:
            return "nature_not_actual_fact"
        if field.get("raw_field_name") not in context.frozen_config.get("known_fields", {}).get(job["dataset_id"], []):
            return "unknown_field"
        return rejections.get(f"{job['dataset_id']}.{field.get('raw_field_name')}", "standard_mapping_missing")
    if field.get("definition_version") != context.field_registry_version:
        return "definition_version_missing_or_mismatched"
    if (field.get("raw_field_name") not in context.frozen_config.get("known_fields", {}).get(job["dataset_id"], [])
            or field.get("standard_field_id") != rule["standard_field"]
            or field.get("dataset_id") != job["dataset_id"]
            or field.get("record_version_id") != record["record_version_id"]):
        return "field_mapping_or_identity_mismatch"
    if not record.get("row_key") or field.get("field_path") != f"$.{field.get('raw_field_name')}":
        return "field_locator_missing_or_mismatched"
    if field["raw_field_name"] not in row or row[field["raw_field_name"]] != field.get("value"):
        return "original_value_mismatch"
    if not field.get("unit") or field["unit"] != rule["stored_unit"]:
        return "unit_missing_or_conflicting"
    if field.get("quality") != "passed" or field.get("nature") != rule["nature"]:
        return "quality_or_nature_not_admissible"
    if rule["nature"] not in {"observed", "deterministic", "announced_plan"}:
        return "nature_not_actual_fact"
    if rule["nature"] == "announced_plan" and job["dataset_id"] != "dividend":
        return "event_definition_missing"
    return None


def _lineage(context, job, record, field, rule, snapshot, contract_hash):
    return {"materialization_version": MATERIALIZATION_VERSION, "contract_hash": contract_hash,
        "storage_namespace_id": context.storage_namespace_id,
        "structured_run_id": context.run_id, "structured_job_id": job["job_id"],
        "structured_dataset_id": job["dataset_id"], "structured_page_id": record.get("page_id"),
        "structured_attempt_id": record.get("_committed_attempt_id"),
        "structured_record_version_id": record["record_version_id"],
        "supersedes_record_version_id": record.get("supersedes_record_version_id"),
        "structured_snapshot_id": record["snapshot_id"], "snapshot_sha256": snapshot.sha256,
        "structured_row_key": record["row_key"], "structured_field_path": field["field_path"],
        "field_value_id": field["field_value_id"], "original_value": field.get("value"),
        "original_unit": rule["original_unit"], "stored_unit": field["unit"],
        "multiplier": rule["multiplier"], "source_definition_id": job["source_definition_id"],
        "source_definition_version": job["source_definition_version"],
        "field_definition_id": (f"{job['dataset_id']}.{field['raw_field_name']}"
            if "input_descriptors" in rule else rule["definition_id"]),
        "field_definition_version": field["definition_version"],
        "field_registry_hash": context.field_registry_hash,
        "source_revision_at": record.get("source_updated_at"),
        "adjustment": (record.get("raw_row") or {}).get("adjustflag"),
        "nature": rule["nature"]}


def _admission(job, record, field, rule, snapshot, observed, available, *, definition_version=None):
    if rule["nature"] == "announced_plan":
        return None
    return StructuredFactAdmission(
        acquisition_method=StructuredAcquisitionMethod.SUPPLIER_STRUCTURED,
        nature=StructuredFactNature(rule["nature"]), quality=StructuredQualityStatus.PASSED,
        source_policy_id=job["source_definition_id"], source_policy_version=job["source_definition_version"],
        field_definition_id=rule["definition_id"], field_definition_version=definition_version or field["definition_version"],
        raw_resource_snapshot_id=record["snapshot_id"], snapshot_sha256=snapshot.sha256,
        row_key=record["row_key"], field_path=field["field_path"], original_value=field["value"],
        original_unit=rule["original_unit"], retrieved_at=observed, available_at=available)


def _source_for_snapshot(snapshot, job, definition):
    return SourceRecord(
        source_id=_id("structured-source", {"source": job["source_definition_id"],
            "version": job["source_definition_version"], "snapshot": snapshot.snapshot_id}),
        name=f"{job['source_definition_id']}结构化响应", source_type="supplier-structured",
        upstream_source_id=definition.get("upstream_identity", job["source_definition_id"]),
        url=snapshot.canonical_url, retrieved_at=_timestamp(snapshot.created_at),
        available_at=_timestamp(snapshot.available_at), document_hash=snapshot.sha256,
        source_definition_id=job["source_definition_id"], source_definition_version=job["source_definition_version"],
        raw_resource_snapshot_id=snapshot.snapshot_id,
        canonical_resource_id=snapshot.canonical_resource_id, published_at=snapshot.published_at,
        notes="已提交结构化字段投影；历史可得性以本地观察为界；未进行人工验收")


def _dimension(dataset, row):
    if dataset == "customers_peer":
        if str(row.get("TYPE_CODE")) not in {"1", "2"} or row.get("RANK") is None or not row.get("ITEM_NAME"):
            return None
        return {"dimension_type": "customer" if str(row['TYPE_CODE']) == '1' else "supplier",
                "dimension_name": str(row['ITEM_NAME']), "dimension_code": "rank:"+str(row['RANK']),
                "accounting_basis": "reported_top_five_amount_no_inferred_denominator"}
    if dataset == "segments":
        if any(row.get(k) in (None, "") for k in ("MAINOP_TYPE", "ITEM_CODE")):
            return None
        return {"dimension_type": f"segment:{row['MAINOP_TYPE']}",
            "dimension_name": str(row.get("ACTUAL_ITEM_NAME") or row.get("ITEM_NAME") or row["ITEM_CODE"]),
            "dimension_code": str(row["ITEM_CODE"]),
            "parent_dimension": str(row["ITEM_PARENT_CODE"]) if row.get("ITEM_PARENT_CODE") else None,
            "accounting_basis": str(row.get("ITEM_LEVEL", ""))}
    if dataset == "float_holders_history" and row.get("HOLDER_NAME"):
        return {"dimension_type": "free_float_holder", "dimension_name": str(row["HOLDER_NAME"]),
            "dimension_code": str(row["HOLDER_CODE"]) if row.get("HOLDER_CODE") else None}
    return None


def _select(items):
    groups = defaultdict(list)
    gaps = set()
    for fact in items:
        dim = (fact.dimension_type, fact.dimension_code, fact.dimension_name, fact.parent_dimension,
               fact.accounting_basis) if isinstance(fact, DimensionalFactRecord) else ()
        key = (fact.ticker, fact.metric_id, fact.period_end, fact.period_type, fact.scope,
               fact.metadata.get("adjustment"), dim)
        groups[key].append(fact)
    selected = []
    for key, bucket in sorted(groups.items(), key=lambda p: str(p[0])):
        by_source = defaultdict(list)
        for f in bucket:
            m = f.metadata
            by_source[(m["structured_dataset_id"], m["source_definition_id"],
                       m["source_definition_version"], m["field_definition_version"])].append(f)
        candidates = []
        conflict = False
        for source_items in by_source.values():
            # Supersession is explicit; equal-value repetitions may coalesce. An
            # unrelated later retrieval is not sufficient proof of a correction.
            superseded = {f.metadata.get("supersedes_record_version_id") for f in source_items}
            by_record = {f.metadata["structured_record_version_id"]: f for f in source_items}
            if any((parent := by_record.get(f.metadata.get("supersedes_record_version_id"))) is not None
                   and (parent.metadata["structured_row_key"] != f.metadata["structured_row_key"]
                        or _available(parent) >= _available(f)) for f in source_items):
                conflict = True
                break
            leaves = [f for f in source_items if f.metadata["structured_record_version_id"] not in superseded]
            if not leaves or len({(f.value, f.unit) for f in leaves}) != 1:
                conflict = True
                break
            candidates.append(max(leaves, key=lambda f: (_available(f), _fact_id(f))))
        if conflict or len({(f.value, f.unit) for f in candidates}) > 1:
            gaps.add(f"source_or_revision_conflict:{key[1]}:{key[2]}:{key[3]}")
            continue
        # Exact duplicates can coalesce; prefer BaoStock for market data under the
        # registered product route. Values from different units never compete.
        candidates.sort(key=lambda f: (0 if f.metadata["structured_dataset_id"] == "baostock_daily" else 1, _fact_id(f)))
        if candidates:
            selected.append(candidates[0])
    return selected, gaps


def _derive(selected):
    series = defaultdict(list)
    output, gaps = [], set()
    direct_ttm = {(_series_key(f), f.period_end) for f in selected
                  if f.period_type == "ttm" and f.metadata["value_kind"] == "flow"}
    for fact in selected:
        m = fact.metadata
        if m["value_kind"] != "flow" or fact.period_type not in {"annual", "cumulative", "single_quarter"}:
            continue
        series[_series_key(fact)].append(fact)
    for series_key, facts in series.items():
        cumulative = {f.period_end: f for f in facts if f.period_type in {"cumulative", "annual"}}
        quarters = {f.period_end: f for f in facts if f.period_type == "single_quarter"}
        inconsistent = set()
        for end in cumulative.keys() & quarters.keys():
            current, direct = cumulative[end], quarters[end]
            previous_end = date(end.year, end.month - 2, 1) - timedelta(days=1)
            previous = cumulative.get(previous_end) if end.month != 3 else None
            if end.month != 3 and previous is None:
                continue
            check = derive_single_quarter(_normalized(current), _normalized(previous) if previous else None,
                fact_id="consistency-check", formula_version=PERIOD_FORMULA_VERSION)
            if check.value != Decimal(direct.metadata["decimal_value"]):
                inconsistent.update(f.fact_id for f in [current, direct, *([previous] if previous else [])])
                gaps.add(f"quarter_cumulative_conflict:{current.metric_id}:{end}")
        if inconsistent:
            selected[:] = [f for f in selected if f.fact_id not in inconsistent]
            continue
        for end, current in sorted(cumulative.items()):
            if end in quarters:
                continue
            previous_end = date(end.year, end.month - 2, 1) - timedelta(days=1)
            previous = cumulative.get(previous_end) if end.month != 3 else None
            inputs = [current] + ([previous] if previous else [])
            identity = {"formula": PERIOD_FORMULA_VERSION, "kind": "single_quarter", "inputs": [f.fact_id for f in inputs]}
            try:
                value = derive_single_quarter(_normalized(current), _normalized(previous) if previous else None,
                    fact_id=_id("structured-derived", identity), formula_version=PERIOD_FORMULA_VERSION)
                result = _derived_fact(value, inputs)
            except ValueError:
                gaps.add(f"single_quarter_missing_or_incompatible:{current.metric_id}:{end}")
                continue
            quarters[end] = result
            output.append(result)
        for end in sorted(set(cumulative) | set(quarters)):
            if (series_key, end) in direct_ttm:
                continue
            wanted = [end]
            for _ in range(3):
                wanted.append(date(wanted[-1].year, wanted[-1].month - 2, 1) - timedelta(days=1))
            inputs = [quarters[d] for d in reversed(wanted) if d in quarters]
            try:
                value = derive_ttm([_normalized(f) for f in inputs],
                    fact_id=_id("structured-derived", {"formula": PERIOD_FORMULA_VERSION, "kind": "ttm", "inputs": [f.fact_id for f in inputs]}),
                    formula_version=PERIOD_FORMULA_VERSION)
                output.append(_derived_fact(value, inputs))
            except ValueError:
                gaps.add(f"ttm_missing_or_incompatible:{series_key[0]}:{end}")
    return output, gaps


def _series_key(fact):
    m = fact.metadata
    family = {"income_fields": "income", "income_quarter": "income",
              "cashflow_fields": "cashflow", "cashflow_quarter": "cashflow"}.get(
                  m["structured_dataset_id"], m["structured_dataset_id"])
    return (fact.metric_id, fact.unit, fact.currency, fact.scope,
            m["source_definition_id"], m["source_definition_version"],
            family, m["field_definition_version"], m["contract_hash"])


def _derive_gross_margin(selected):
    """Existing metrics.json gross_margin formula on same-source income fields.

    Missing/zero revenue is a gap, negative revenue or profit is not clamped.
    Period and source matching precede arithmetic. This is a mechanical formula,
    not acceptance of the skeleton income-statement methodology.
    """
    from analysis.formulas import gross_margin
    groups = defaultdict(dict)
    for fact in selected:
        m = fact.metadata
        if m.get("interpretation_contract_id") == "eastmoney-financial-interpretation-v1.0.0" and m.get("statement_org_type") != "通用":
            continue
        if (fact.metric_id not in {"operating_income", "operating_cost"}
                or m.get("structured_dataset_id") not in {"income_fields", "income_quarter"}):
            continue
        key = (fact.period_start, fact.period_end, fact.period_type, fact.unit, fact.currency,
               fact.scope, m["source_definition_id"], m["source_definition_version"],
               m["structured_dataset_id"], m["field_definition_version"], m["contract_hash"])
        groups[key][fact.metric_id] = fact
    result, gaps = [], set()
    for key, values in sorted(groups.items(), key=lambda p: str(p[0])):
        if "operating_income" not in values or "operating_cost" not in values:
            continue
        revenue, cost = values["operating_income"], values["operating_cost"]
        ratio = gross_margin(revenue.value, cost.value)
        if ratio is None or not math.isfinite(ratio):
            gaps.add(f"gross_margin_zero_denominator:{revenue.period_end}:{revenue.period_type}")
            continue
        formula = "structured-gross-margin-v1.0.0"
        ids = [revenue.fact_id, cost.fact_id]
        result.append(FactRecord(
            fact_id=_id("structured-derived", {"formula": formula, "inputs": ids}),
            ticker=revenue.ticker, metric_id="gross_margin", value=ratio, unit="ratio",
            currency=revenue.currency, period_start=revenue.period_start, period_end=revenue.period_end,
            period_type=revenue.period_type, scope=revenue.scope,
            as_of=max(revenue.as_of, cost.as_of),
            source_ids=sorted(set(revenue.source_ids + cost.source_ids)),
            verification_status=VerificationStatus.DERIVED, method_ref=formula,
            derived_from_fact_ids=ids, metadata={
                "materialization_version": MATERIALIZATION_VERSION,
                "structured_run_id": revenue.metadata["structured_run_id"],
                "storage_namespace_id": revenue.metadata["storage_namespace_id"],
                "contract_hash": revenue.metadata["contract_hash"],
                "value_kind": "ratio", "nature": "deterministic", "formula_version": formula,
                "formula": "(operating_income-operating_cost)/operating_income",
                "formula_authority": "config/methods/metrics.json:gross_margin",
                "input_fact_ids": ids, "requires_materialization_selection": True,
                "available_at": max(revenue.as_of, cost.as_of).isoformat()}))
    return result, gaps


def _normalized(f):
    return NormalizedValue(f.fact_id, f.metric_id, Decimal(f.metadata["decimal_value"]),
        f.unit, f.currency, f.period_start, f.period_end, PeriodKind(f.period_type), ValueKind.FLOW, f.scope)


def _derived_fact(value, inputs):
    if not math.isfinite(float(value.value)):
        raise ValueError("derived value cannot be represented as a finite fact")
    metadata = {k: v for k, v in inputs[0].metadata.items() if k in {
        "materialization_version", "structured_run_id", "storage_namespace_id", "contract_hash",
        "field_registry_hash", "requires_materialization_selection"}}
    metadata.update(decimal_value=format(value.value, "f"), value_kind="flow", nature="deterministic",
        formula_version=value.formula_version, formula_kind=value.period_kind.value,
        input_fact_ids=[i.fact_id for i in value.inputs],
        input_values=[{"fact_id": i.fact_id, "value": str(i.value), "unit": i.unit,
                       "period_end": i.period_end.isoformat()} for i in value.inputs],
        available_at=max(f.as_of for f in inputs).isoformat(),
        point_in_time_basis="max_input_available_at", historical_publication_proven=False)
    return FactRecord(fact_id=value.fact_id, ticker=inputs[0].ticker, metric_id=value.field_id,
        value=float(value.value), unit=value.unit, currency=value.currency,
        period_start=value.period_start, period_end=value.period_end, period_type=value.period_kind.value,
        as_of=max(f.as_of for f in inputs), scope=value.scope,
        source_ids=sorted({s for f in inputs for s in f.source_ids}),
        verification_status=VerificationStatus.DERIVED,
        derived_from_fact_ids=[i.fact_id for i in value.inputs], method_ref=value.formula_version,
        metadata=metadata)


def _period_start(end, kind):
    if kind in {"cumulative", "annual"}:
        return date(end.year, 1, 1)
    if kind == "single_quarter":
        return date(end.year, end.month - 2, 1)
    if kind == "ttm":
        start_end = date(end.year - 1, end.month, end.day)
        return start_end + timedelta(days=1)
    return None


def _period_end(value, row, dataset):
    # Only the frozen dataset's business date; never fallback to NOTICE_DATE.
    dates = [name for name in dataset.get("date_fields", []) if name not in {"NOTICE_DATE", "PUBLISH_DATE"}]
    business = next((row[n] for n in dates if row.get(n) not in (None, "")), None)
    try:
        parsed = _business_date(value)
        if business is None or _business_date(business) != parsed:
            return None
        return parsed
    except ValueError:
        return None


def _business_date(value):
    text = str(value).strip()
    return date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text).date()


def _event_date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Chinese supplier date-only publications have an explicit source zone;
        # conservatively become available at the end of that local day.
        if parsed.tzinfo is None:
            parsed = parsed.replace(hour=23, minute=59, second=59,
                tzinfo=timezone(timedelta(hours=8)))
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _timestamp(value):
    if value is None:
        raise ValueError("required timestamp missing")
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _number(value):
    if value is None or isinstance(value, bool) or not str(value).strip():
        raise ValueError("numeric value missing")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite numeric value")
    return number


def _id(prefix, value):
    return prefix + "-" + canonical_sha256(value)[:32]


def _get(value, key):
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key)


def _enum(value):
    return getattr(value, "value", value)


def _available(fact):
    return fact.available_at if isinstance(fact, DimensionalFactRecord) else fact.as_of


def _fact_id(fact):
    return fact.dimensional_fact_id if isinstance(fact, DimensionalFactRecord) else fact.fact_id
