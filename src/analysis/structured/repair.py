from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .storage import StructuredRunContext, canonical_json, canonical_sha256


_SUCCESS_OUTCOMES = {"success", "unchanged", "no_data"}


class RepairManifestError(ValueError):
    """A repair authorization is malformed, stale, or bound elsewhere."""


class FrozenRepairModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RepairManifestItem(FrozenRepairModel):
    job_id: str = Field(min_length=1)
    plan_item_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    source_definition_id: str = Field(min_length=1)
    source_definition_version: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    scope_key: str = Field(min_length=1)
    time_start: str | None = None
    time_end: str | None = None
    baseline_attempt_count: int = Field(ge=1)
    baseline_max_attempts: int = Field(ge=1)
    baseline_failure_reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_failure_reasons(self) -> "RepairManifestItem":
        if self.baseline_attempt_count < self.baseline_max_attempts:
            raise ValueError("repair item baseline is not terminally exhausted")
        if any(not value for value in self.baseline_failure_reasons):
            raise ValueError("repair failure reasons must be non-empty")
        if tuple(sorted(set(self.baseline_failure_reasons))) != self.baseline_failure_reasons:
            raise ValueError("repair failure reasons must be unique and sorted")
        return self


class RepairManifest(FrozenRepairModel):
    schema_version: Literal["structured-repair-manifest.v1"]
    manifest_id: str = Field(pattern=r"^structured-repair-[0-9a-f]{24}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    storage_namespace_id: str = Field(min_length=1)
    frozen_context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    dataset_filters: tuple[str, ...] = Field(min_length=1)
    reason_filters: tuple[str, ...] = Field(min_length=1)
    max_additional_attempts: int = Field(default=2, ge=1, le=2)
    items: tuple[RepairManifestItem, ...] = ()

    @field_validator("dataset_filters", "reason_filters")
    @classmethod
    def validate_nonempty_filters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("repair filters must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_identity_and_uniqueness(self) -> "RepairManifest":
        if tuple(sorted(set(self.dataset_filters))) != self.dataset_filters:
            raise ValueError("repair dataset filters must be unique and sorted")
        if tuple(sorted(set(self.reason_filters))) != self.reason_filters:
            raise ValueError("repair reason filters must be unique and sorted")
        job_ids = tuple(item.job_id for item in self.items)
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("repair manifest contains duplicate jobs")
        if tuple(sorted(job_ids)) != job_ids:
            raise ValueError("repair manifest jobs must be sorted")
        datasets = set(self.dataset_filters)
        reasons = set(self.reason_filters)
        for item in self.items:
            if item.dataset_id not in datasets:
                raise ValueError("repair item is outside the dataset filters")
            if not reasons.intersection(item.baseline_failure_reasons):
                raise ValueError("repair item is outside the reason filters")
        expected = canonical_sha256(self.hash_payload())
        if self.manifest_sha256 != expected:
            raise ValueError("repair manifest hash mismatch")
        if self.manifest_id != f"structured-repair-{expected[:24]}":
            raise ValueError("repair manifest id mismatch")
        return self

    def hash_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json", exclude={"manifest_id", "manifest_sha256"}
        )

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        storage_namespace_id: str,
        frozen_context_hash: str,
        code_revision: str,
        dataset_filters: Iterable[str],
        reason_filters: Iterable[str],
        items: Iterable[RepairManifestItem],
        max_additional_attempts: int = 2,
    ) -> "RepairManifest":
        payload = {
            "schema_version": "structured-repair-manifest.v1",
            "run_id": str(run_id),
            "storage_namespace_id": str(storage_namespace_id),
            "frozen_context_hash": str(frozen_context_hash),
            "code_revision": str(code_revision),
            "dataset_filters": tuple(sorted(set(str(item) for item in dataset_filters))),
            "reason_filters": tuple(sorted(set(str(item) for item in reason_filters))),
            "max_additional_attempts": int(max_additional_attempts),
            "items": tuple(sorted(items, key=lambda item: item.job_id)),
        }
        digest = canonical_sha256(
            {
                **payload,
                "items": [item.model_dump(mode="json") for item in payload["items"]],
            }
        )
        return cls.model_validate(
            {
                **payload,
                "manifest_id": f"structured-repair-{digest[:24]}",
                "manifest_sha256": digest,
            }
        )


@dataclass(frozen=True)
class RepairCandidate:
    job_id: str
    dataset_id: str
    state: str
    attempt_count: int
    failure_count: int
    committed_page_count: int
    committed_record_count: int
    reusable_snapshot_ids: tuple[str, ...]
    next_retry_ordinal: int


def write_repair_manifest(path: Path | str, manifest: RepairManifest) -> None:
    target = Path(path)
    if target.exists():
        existing = load_repair_manifest(target)
        if existing != manifest:
            raise RepairManifestError(
                "repair manifest path already contains different content"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(
        canonical_json(manifest.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    os.replace(temp, target)


def load_repair_manifest(path: Path | str) -> RepairManifest:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        return RepairManifest.model_validate_json(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RepairManifestError(f"invalid repair manifest: {exc}") from exc


def plan_repair(
    *,
    bridge: Any,
    storage: Any,
    repository: Any,
    run_id: str,
    dataset_filters: Sequence[str],
    reason_filters: Sequence[str],
    code_revision: str,
) -> RepairManifest:
    if not dataset_filters or not reason_filters:
        raise RepairManifestError("repair plan requires dataset and reason filters")
    context = bridge.prepare_execution(run_id)
    statuses = {item.job_id: item for item in bridge.status(run_id).jobs}
    jobs = storage.list_jobs(run_id, limit=None)
    attempts = repository.list_attempts(run_id=run_id, limit=None)
    attempts_by_plan = _attempts_by_plan(attempts)
    datasets = set(str(item) for item in dataset_filters)
    reasons = set(str(item) for item in reason_filters)
    items: list[RepairManifestItem] = []
    for job in jobs:
        job_id = str(job["job_id"])
        status = statuses[job_id]
        if status.state != "failed" or str(job["dataset_id"]) not in datasets:
            continue
        job_attempts = attempts_by_plan.get(str(job["plan_item_id"]), ())
        failure_reasons = _failure_reasons(repository, job_attempts)
        if not reasons.intersection(failure_reasons):
            continue
        items.append(
            RepairManifestItem(
                job_id=job_id,
                plan_item_id=str(job["plan_item_id"]),
                company_id=str(job["company_id"]),
                ticker=str(job["ticker"]),
                dataset_id=str(job["dataset_id"]),
                source_definition_id=str(job["source_definition_id"]),
                source_definition_version=str(job["source_definition_version"]),
                purpose=str(job["purpose"]),
                scope_key=str(job["scope_key"]),
                time_start=_optional_text(job.get("time_start")),
                time_end=_optional_text(job.get("time_end")),
                baseline_attempt_count=len(job_attempts),
                baseline_max_attempts=int(job["max_attempts"]),
                baseline_failure_reasons=tuple(sorted(failure_reasons)),
            )
        )
    return RepairManifest.create(
        run_id=run_id,
        storage_namespace_id=context.storage_namespace_id,
        frozen_context_hash=context.content_hash,
        code_revision=code_revision,
        dataset_filters=dataset_filters,
        reason_filters=reason_filters,
        items=items,
    )


def repair_status(
    manifest: RepairManifest,
    *,
    bridge: Any,
    storage: Any,
    repository: Any,
    expected_code_revision: str | None = None,
) -> dict[str, Any]:
    context = _assert_manifest_binding(
        manifest,
        bridge=bridge,
        storage=storage,
        expected_code_revision=expected_code_revision,
    )
    statuses = {item.job_id: item for item in bridge.status(manifest.run_id).jobs}
    attempts = repository.list_attempts(run_id=manifest.run_id, limit=None)
    attempts_by_plan = _attempts_by_plan(attempts)
    job_map = {str(item["job_id"]): item for item in storage.list_jobs(manifest.run_id, limit=None)}
    details: list[dict[str, Any]] = []
    for item in manifest.items:
        job = job_map.get(item.job_id)
        if job is None:
            raise RepairManifestError(f"repair job is missing: {item.job_id}")
        _assert_item_binding(item, job)
        job_attempts = attempts_by_plan.get(item.plan_item_id, ())
        if len(job_attempts) < item.baseline_attempt_count:
            raise RepairManifestError(
                f"repair attempt history shrank for job {item.job_id}"
            )
        outcomes = _terminal_outcomes(repository, job_attempts)
        baseline_outcomes = _terminal_outcomes(
            repository, job_attempts[: item.baseline_attempt_count]
        )
        if baseline_outcomes.intersection(_SUCCESS_OUTCOMES):
            raise RepairManifestError(
                f"repair baseline was not terminally failed for job {item.job_id}"
            )
        baseline_reasons = tuple(
            sorted(
                _failure_reasons(
                    repository, job_attempts[: item.baseline_attempt_count]
                )
            )
        )
        if baseline_reasons != item.baseline_failure_reasons:
            raise RepairManifestError(
                f"repair baseline failure reasons changed for job {item.job_id}"
            )
        if not set(manifest.reason_filters).intersection(baseline_reasons):
            raise RepairManifestError(
                f"repair reason filter does not match job {item.job_id}"
            )
        current = statuses[item.job_id]
        additional = len(job_attempts) - item.baseline_attempt_count
        successful = "no_data" if "no_data" in outcomes else (
            "succeeded" if outcomes.intersection({"success", "unchanged"}) else None
        )
        state = successful or (
            "exhausted"
            if additional >= manifest.max_additional_attempts
            else "ready"
        )
        next_retry = max(
            (int(_value(attempt, "retry_ordinal", 0)) for attempt in job_attempts),
            default=-1,
        ) + 1
        coverage = [
            value
            for value in storage.list_acquisition_coverage(
                run_id=manifest.run_id,
                company_id=item.company_id,
                dataset_id=item.dataset_id,
                limit=None,
            )
            if value.get("job_id") == item.job_id
        ]
        latest_coverage = coverage[-1] if coverage else {}
        pages = storage.list_pages(item.job_id)
        details.append(
            {
                "job_id": item.job_id,
                "company_id": item.company_id,
                "ticker": item.ticker,
                "dataset_id": item.dataset_id,
                "baseline_failure_reasons": list(item.baseline_failure_reasons),
                "state": state,
                "ordinary_state": current.state,
                "baseline_attempt_count": item.baseline_attempt_count,
                "current_attempt_count": len(job_attempts),
                "repair_attempt_count": additional,
                "remaining_attempts": max(
                    0, manifest.max_additional_attempts - additional
                ),
                "next_retry_ordinal": next_retry,
                "page_count": len(pages),
                "record_count": current.committed_record_count,
                "pagination_issues": sorted(
                    {
                        str(issue)
                        for page in pages
                        for issue in (page.get("pagination_issues") or ())
                    }
                ),
                "coverage_status": latest_coverage.get("status"),
                "safe_through": latest_coverage.get("safe_through"),
            }
        )
    counts = {
        state: sum(item["state"] == state for item in details)
        for state in ("ready", "succeeded", "no_data", "exhausted")
    }
    return {
        "schema_version": "structured-repair-status.v1",
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "run_id": manifest.run_id,
        "storage_namespace_id": context.storage_namespace_id,
        "frozen_context_hash": context.content_hash,
        "code_revision": manifest.code_revision,
        "source_status": "not_probed",
        "target_count": len(details),
        "counts": counts,
        "groups": _status_groups(details),
        "items": details,
        "performed_network_io": False,
        "manual_acceptance": "pending_independent",
    }


def repair_candidates(
    manifest: RepairManifest,
    *,
    bridge: Any,
    storage: Any,
    repository: Any,
    expected_code_revision: str,
) -> tuple[RepairCandidate, ...]:
    status = repair_status(
        manifest,
        bridge=bridge,
        storage=storage,
        repository=repository,
        expected_code_revision=expected_code_revision,
    )
    bridge_statuses = {item.job_id: item for item in bridge.status(manifest.run_id).jobs}
    return tuple(
        RepairCandidate(
            job_id=str(item["job_id"]),
            dataset_id=str(item["dataset_id"]),
            state="repair_ready",
            attempt_count=int(item["current_attempt_count"]),
            failure_count=bridge_statuses[str(item["job_id"])].failure_count,
            committed_page_count=bridge_statuses[str(item["job_id"])].committed_page_count,
            committed_record_count=bridge_statuses[str(item["job_id"])].committed_record_count,
            reusable_snapshot_ids=bridge_statuses[str(item["job_id"])].reusable_snapshot_ids,
            next_retry_ordinal=int(item["next_retry_ordinal"]),
        )
        for item in status["items"]
        if item["state"] == "ready"
    )


def _assert_manifest_binding(
    manifest: RepairManifest,
    *,
    bridge: Any,
    storage: Any,
    expected_code_revision: str | None,
) -> StructuredRunContext:
    context = bridge.prepare_execution(manifest.run_id)
    if storage.storage_namespace_id != manifest.storage_namespace_id:
        raise RepairManifestError("repair storage namespace mismatch")
    if context.storage_namespace_id != manifest.storage_namespace_id:
        raise RepairManifestError("repair run namespace mismatch")
    if context.content_hash != manifest.frozen_context_hash:
        raise RepairManifestError("repair frozen context hash mismatch")
    if expected_code_revision is not None and expected_code_revision != manifest.code_revision:
        raise RepairManifestError("repair code revision mismatch")
    return context


def _assert_item_binding(item: RepairManifestItem, job: Mapping[str, Any]) -> None:
    expected = {
        "plan_item_id": item.plan_item_id,
        "company_id": item.company_id,
        "ticker": item.ticker,
        "dataset_id": item.dataset_id,
        "source_definition_id": item.source_definition_id,
        "source_definition_version": item.source_definition_version,
        "purpose": item.purpose,
        "scope_key": item.scope_key,
        "time_start": item.time_start,
        "time_end": item.time_end,
        "max_attempts": item.baseline_max_attempts,
    }
    for name, value in expected.items():
        actual = job.get(name)
        actual = _optional_text(actual) if name in {"time_start", "time_end"} else actual
        if str(actual) != str(value):
            raise RepairManifestError(
                f"repair job binding mismatch: {item.job_id}:{name}"
            )


def _attempts_by_plan(attempts: Iterable[Any]) -> dict[str, tuple[Any, ...]]:
    values: dict[str, list[Any]] = {}
    for attempt in attempts:
        values.setdefault(
            str(_value(attempt, "physical_query_plan_item_id")), []
        ).append(attempt)
    return {key: tuple(value) for key, value in values.items()}


def _terminal_outcomes(repository: Any, attempts: Iterable[Any]) -> set[str]:
    return {
        str(_enum_value(_value(event, "outcome")))
        for attempt in attempts
        for event in repository.list_attempt_events(str(_value(attempt, "attempt_id")))
        if _value(event, "outcome") is not None
    }


def _failure_reasons(repository: Any, attempts: Iterable[Any]) -> set[str]:
    values: set[str] = set()
    for attempt in attempts:
        for event in repository.list_attempt_events(str(_value(attempt, "attempt_id"))):
            outcome = _enum_value(_value(event, "outcome"))
            if outcome is None or outcome in _SUCCESS_OUTCOMES:
                continue
            values.add(str(_value(event, "reason_code") or "unspecified_failure"))
    return values


def _status_groups(items: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {
        "company": {},
        "dataset": {},
        "reason": {},
    }
    for item in items:
        for group, values in (
            ("company", (str(item["company_id"]),)),
            ("dataset", (str(item["dataset_id"]),)),
            ("reason", tuple(str(value) for value in item["baseline_failure_reasons"])),
        ):
            for value in values:
                grouped[group][value] = grouped[group].get(value, 0) + 1
    return grouped


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)
