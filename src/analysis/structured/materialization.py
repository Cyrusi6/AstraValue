from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from analysis.models import (
    DimensionalFactRecord,
    FactRecord,
    SourceRecord,
    StructuredAcquisitionMethod,
    StructuredFactAdmission,
    StructuredFactNature,
    StructuredQualityStatus,
    VerificationStatus,
)

from .mappings import FIELD_RULES, FieldRule
from .storage import StructuredStorage


MATERIALIZATION_VERSION = "structured-materialization-v1.0.0"


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    run_id: str
    facts: tuple[FactRecord, ...]
    dimensional_facts: tuple[DimensionalFactRecord, ...]
    sources: tuple[SourceRecord, ...]
    candidates: int
    skipped: int
    gaps: tuple[str, ...]
    materialization_hash: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "materialization_version": MATERIALIZATION_VERSION,
            "materialization_hash": self.materialization_hash,
            "fact_count": len(self.facts),
            "dimensional_fact_count": len(self.dimensional_facts),
            "source_count": len(self.sources),
            "candidate_count": self.candidates,
            "skipped_count": self.skipped,
            "gaps": list(self.gaps),
        }


class StructuredFactMaterializer:
    """Project committed structured rows into the existing report fact model.

    The structured tables remain authoritative.  This class is a deterministic
    read/transform operation: it never contacts an upstream source and never
    changes a structured page, record, snapshot or coverage row.
    """

    def __init__(self, storage: StructuredStorage, repository: Any) -> None:
        self.storage = storage
        self.repository = repository

    def materialize(
        self,
        run_id: str,
        *,
        as_of: datetime | None = None,
        strict_historical: bool = False,
    ) -> MaterializationResult:
        context = self.storage.get_run_context(run_id)
        cutoff = _aware(as_of) if as_of is not None else None
        jobs = self.storage.list_jobs(run_id, limit=None)
        facts_by_key: dict[tuple[str, str, str, str], FactRecord] = {}
        sources: dict[str, SourceRecord] = {}
        snapshots: dict[str, Any] = {}
        candidates = 0
        skipped = 0
        gaps: set[str] = set()
        jobs_by_id = {str(job["job_id"]): job for job in jobs}
        records = self.storage.list_records_for_jobs(jobs_by_id, limit=None)
        fields_by_record: dict[str, list[dict[str, Any]]] = {}
        all_fields = self.storage.list_record_fields_for_records(
            (str(item["record_version_id"]) for item in records), limit=None
        )
        for item in all_fields:
            fields_by_record.setdefault(str(item["record_version_id"]), []).append(item)

        for record in records:
            job = jobs_by_id.get(str(record["job_id"]))
            if job is None:
                continue
            dataset_id = str(job["dataset_id"])
            available_at = _parse_datetime(record.get("available_at"))
            if cutoff is not None and strict_historical and available_at > cutoff:
                skipped += 1
                continue
            raw_row = record.get("raw_row") or {}
            for field in fields_by_record.get(str(record["record_version_id"]), ()):
                candidates += 1
                standard = field.get("standard_field_id")
                value = field.get("value")
                rule = FIELD_RULES.get((dataset_id, str(field.get("raw_field_name"))))
                if not standard or rule is None:
                    skipped += 1
                    continue
                if field.get("quality") != "passed" or field.get("nature") not in {
                    "observed",
                    "deterministic",
                }:
                    skipped += 1
                    gaps.add(f"{dataset_id}.{field.get('raw_field_name')}:field_not_admissible")
                    continue
                numeric = _number(value)
                if numeric is None:
                    skipped += 1
                    gaps.add(f"{standard}:value_missing_or_non_numeric")
                    continue
                snapshot_id = str(record["snapshot_id"])
                snapshot = snapshots.get(snapshot_id)
                if snapshot is None:
                    snapshot = self.repository.get_raw_resource_snapshot(snapshot_id)
                    snapshots[snapshot_id] = snapshot
                source = _source_for_snapshot(snapshot, job)
                sources[source.source_id] = source
                period_end = _period_end(field.get("period_key"), raw_row)
                if period_end is None:
                    skipped += 1
                    gaps.add(f"{standard}:period_missing")
                    continue
                converted = numeric * _decimal(rule.multiplier)
                fact = self._fact(
                    context=context,
                    job=job,
                    record=record,
                    field=field,
                    rule=rule,
                    value=converted,
                    period_end=period_end,
                    source=source,
                    snapshot=snapshot,
                )
                key = (fact.metric_id, fact.period_end.isoformat(), fact.scope, fact.period_type)
                current = facts_by_key.get(key)
                if current is None or _priority(fact) < _priority(current):
                    facts_by_key[key] = fact

        facts = tuple(sorted(facts_by_key.values(), key=lambda item: (
            item.metric_id, item.period_end or date.min, item.as_of, item.fact_id
        )))
        payload = {
            "run_id": run_id,
            "version": MATERIALIZATION_VERSION,
            "facts": [item.model_dump(mode="json") for item in facts],
            "sources": [item.model_dump(mode="json") for item in sorted(sources.values(), key=lambda item: item.source_id)],
            "gaps": sorted(gaps),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return MaterializationResult(
            run_id=run_id,
            facts=facts,
            dimensional_facts=(),
            sources=tuple(sorted(sources.values(), key=lambda item: item.source_id)),
            candidates=candidates,
            skipped=skipped,
            gaps=tuple(sorted(gaps)),
            materialization_hash=digest,
        )

    @staticmethod
    def _fact(*, context: Any, job: Mapping[str, Any], record: Mapping[str, Any],
              field: Mapping[str, Any], rule: FieldRule, value: Decimal,
              period_end: date, source: SourceRecord, snapshot: Any) -> FactRecord:
        raw_name = str(field["raw_field_name"])
        row_key = str(record["row_key"])
        identity = {
            "run_id": context.run_id,
            "dataset_id": job["dataset_id"],
            "row_key": row_key,
            "standard_field": field["standard_field_id"],
            "period_end": period_end.isoformat(),
            "snapshot": str(record["snapshot_id"]),
        }
        fact_id = "structured-fact-" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
        nature = StructuredFactNature(str(field["nature"]))
        admission = StructuredFactAdmission(
            acquisition_method=StructuredAcquisitionMethod.SUPPLIER_STRUCTURED,
            nature=nature,
            quality=StructuredQualityStatus.PASSED,
            source_policy_id=str(job["source_definition_id"]),
            source_policy_version=str(job["source_definition_version"]),
            field_definition_id=rule.definition_id,
            field_definition_version=str(field.get("definition_version") or context.field_registry_version),
            raw_resource_snapshot_id=str(record["snapshot_id"]),
            snapshot_sha256=str(snapshot.sha256),
            row_key=row_key,
            field_path=str(field["field_path"]),
            original_value=field.get("value"),
            original_unit=field.get("unit"),
            retrieved_at=_parse_datetime(record.get("observed_at") or record.get("available_at")),
            available_at=_parse_datetime(record.get("available_at")),
        )
        metadata = {
            "materialization_version": MATERIALIZATION_VERSION,
            "structured_run_id": context.run_id,
            "structured_job_id": job["job_id"],
            "structured_dataset_id": job["dataset_id"],
            "structured_record_version_id": record["record_version_id"],
            "structured_snapshot_id": record["snapshot_id"],
            "structured_row_key": row_key,
            "structured_field_path": field["field_path"],
        }
        return FactRecord(
            fact_id=fact_id,
            ticker=str(context.ticker),
            metric_id=str(field["standard_field_id"]),
            value=float(value) if math.isfinite(float(value)) else None,
            unit=str(rule.unit),
            currency="CNY",
            period_start=None,
            period_end=period_end,
            period_type=_period_type(rule),
            as_of=_parse_datetime(record.get("available_at")),
            scope=rule.scope,
            original_label=raw_name,
            source_ids=[source.source_id],
            verification_status=VerificationStatus.SUPPLIER_DIRECT,
            structured_admission=admission,
            metadata=metadata,
        )


def _source_for_snapshot(snapshot: Any, job: Mapping[str, Any]) -> SourceRecord:
    source_id = "structured-source-" + hashlib.sha256(
        f"{job['source_definition_id']}@{job['source_definition_version']}:{snapshot.snapshot_id}".encode()
    ).hexdigest()[:32]
    return SourceRecord(
        source_id=source_id,
        name=f"{job['source_definition_id']}结构化响应",
        source_type="structured-supplier",
        upstream_source_id=str(job["source_definition_id"]),
        url=getattr(snapshot, "canonical_url", None),
        retrieved_at=_parse_datetime(getattr(snapshot, "created_at", None)),
        available_at=_parse_datetime(getattr(snapshot, "available_at", None)),
        document_hash=str(snapshot.sha256),
        authority_level=3,
        source_definition_id=str(job["source_definition_id"]),
        source_definition_version=str(job["source_definition_version"]),
        raw_resource_snapshot_id=str(snapshot.snapshot_id),
        canonical_resource_id=getattr(snapshot, "canonical_resource_id", None),
        published_at=getattr(snapshot, "published_at", None),
        notes="由已提交结构化运行物化；不代表人工黄金样本验收",
    )


def _period_type(rule: FieldRule) -> str:
    value = rule.period_kind.value
    return {"cumulative": "cumulative", "single_quarter": "single_quarter", "ttm": "ttm", "instant": "instant", "market_quote": "market_quote"}.get(value, value)


def _priority(fact: FactRecord) -> tuple[int, str]:
    dataset = str(fact.metadata.get("structured_dataset_id", ""))
    if fact.metric_id.startswith(("market_", "pe_", "pb_", "ps_", "pcf_", "turnover_", "price_")):
        return (0 if dataset in {"market_cap", "baostock_daily"} else 1, fact.fact_id)
    return (0 if dataset.startswith(("income", "balance", "cashflow", "em_")) else 1, fact.fact_id)


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or not str(value).strip():
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _decimal(value: Any) -> Decimal:
    return Decimal("1") if value is None else Decimal(str(value))


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    if value is None:
        return datetime.now(timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    return _aware(datetime.fromisoformat(text))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _period_end(value: Any, row: Mapping[str, Any]) -> date | None:
    candidate = value
    if candidate in (None, ""):
        for name in ("REPORT_DATE", "date", "trade_date", "交易日期", "NOTICE_DATE"):
            if row.get(name) not in (None, ""):
                candidate = row[name]
                break
    if candidate in (None, ""):
        return None
    text = str(candidate).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


__all__ = ["MATERIALIZATION_VERSION", "MaterializationResult", "StructuredFactMaterializer"]
