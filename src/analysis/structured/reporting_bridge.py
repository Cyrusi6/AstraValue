"""把冻结的八步轻量包转换为现有报告请求。"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from analysis.models import (
    DimensionalFactRecord,
    FactRecord,
    ReportCreateRequest,
    SourceRecord,
    VerificationStatus,
)

from .consumption import is_fact_consumable
from .scope import LITE_PROFILE_ID, load_research_profile
from .storage import canonical_sha256


REPORT_BRIDGE_VERSION = "eight-step-lite-report-bridge-v1.0.0"
METRIC_ALIASES = {
    "operating_income": "revenue",
    "operating_cost": "cost_of_revenue",
    "parent_net_profit": "net_income_parent",
    "deducted_parent_net_profit": "net_income_excl",
    "research_expense": "rd_expense",
    "long_asset_cash_purchase": "capital_expenditure",
}


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"report_pack_object_required:{path.name}")
    return value


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"report_pack_jsonl_object_required:{path}:{line_number}")
            yield value


def _verify_descriptor(value: Mapping[str, Any], *, label: str) -> Path:
    path_text = value.get("path")
    expected = value.get("sha256")
    if not isinstance(path_text, str) or not isinstance(expected, str):
        raise ValueError(f"report_pack_file_descriptor_invalid:{label}")
    path = Path(path_text).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"report_pack_input_missing:{label}:{path}")
    if _hash_file(path) != expected:
        raise ValueError(f"report_pack_input_hash_mismatch:{label}")
    return path


def _validate_pack(pack_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    pack_dir = pack_dir.resolve()
    manifest = _read_json(pack_dir / "manifest.json")
    for name, expected in manifest.get("output_hashes", {}).items():
        path = (pack_dir / str(name)).resolve()
        if not path.is_relative_to(pack_dir):
            raise ValueError(f"report_pack_output_path_escape:{name}")
        if not path.is_file() or _hash_file(path) != expected:
            raise ValueError(f"report_pack_output_hash_mismatch:{name}")
    for index, source_input in enumerate(manifest.get("source_inputs", ())):
        for name, descriptor in (source_input.get("files") or {}).items():
            _verify_descriptor(descriptor, label=f"source_inputs[{index}].files.{name}")
        for manifest_index, descriptor in enumerate(source_input.get("manifests", ())):
            _verify_descriptor(
                descriptor,
                label=f"source_inputs[{index}].manifests[{manifest_index}]",
            )
    for group in ("auxiliary_inputs", "peer_inputs"):
        for index, item in enumerate(manifest.get(group, ())):
            if "files" in item:
                for name, descriptor in (item.get("files") or {}).items():
                    _verify_descriptor(descriptor, label=f"{group}[{index}].files.{name}")
            elif "path" in item:
                _verify_descriptor(item, label=f"{group}[{index}]")

    core = _read_json(pack_dir / "core-pack.json")
    coverage = _read_json(pack_dir / "core-coverage.json")
    identity_fields = ("pack_id", "profile_id", "ticker", "as_of", "status")
    for field in identity_fields:
        if core.get(field) != manifest.get(field):
            raise ValueError(f"report_pack_identity_mismatch:{field}")
    for field in ("profile_id", "profile_sha256", "ticker", "as_of", "periods"):
        if coverage.get(field) != manifest.get(field):
            raise ValueError(f"report_pack_coverage_mismatch:{field}")
    if manifest.get("status") != "ready":
        raise ValueError(f"report_pack_status_not_reportable:{manifest.get('status')}")
    return manifest, core, coverage


def _full_projection_records(
    manifest: Mapping[str, Any], ticker: str
) -> tuple[dict[str, FactRecord], dict[str, DimensionalFactRecord]]:
    facts: dict[str, FactRecord] = {}
    dimensions: dict[str, DimensionalFactRecord] = {}
    for source_input in manifest.get("source_inputs", ()):
        descriptor = (source_input.get("files") or {}).get("coverage-facts.jsonl")
        if not descriptor:
            continue
        path = Path(str(descriptor["path"])).resolve()
        for payload in _read_jsonl(path):
            if payload.get("ticker") != ticker:
                raise ValueError("report_pack_projection_company_mismatch")
            if "fact_id" in payload:
                record = FactRecord.model_validate(payload)
                current = facts.get(record.fact_id)
                if current is not None and current.model_dump(mode="json") != record.model_dump(mode="json"):
                    raise ValueError(f"report_pack_conflicting_fact:{record.fact_id}")
                facts[record.fact_id] = record
            elif "dimensional_fact_id" in payload:
                record = DimensionalFactRecord.model_validate(payload)
                current = dimensions.get(record.dimensional_fact_id)
                if current is not None and current.model_dump(mode="json") != record.model_dump(mode="json"):
                    raise ValueError(
                        f"report_pack_conflicting_dimensional_fact:{record.dimensional_fact_id}"
                    )
                dimensions[record.dimensional_fact_id] = record
    return facts, dimensions


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _core_fact_index(core: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for metric in core.get("metrics", ()):
        candidates = [metric.get("fact"), (metric.get("yoy") or {}).get("prior_fact")]
        for payload in candidates:
            if not isinstance(payload, dict) or not payload.get("fact_id"):
                continue
            payload = {
                key: value for key, value in payload.items() if key != "reference_id"
            }
            fact_id = str(payload["fact_id"])
            current = result.get(fact_id)
            if current is not None and current != payload:
                raise ValueError(f"report_pack_conflicting_core_fact:{fact_id}")
            result[fact_id] = payload
    return result


def _assert_core_matches_full(core: Mapping[str, Any], full: FactRecord) -> None:
    checks = {
        "metric_id": full.metric_id,
        "period_end": full.period_end.isoformat() if full.period_end else None,
        "period_start": full.period_start.isoformat() if full.period_start else None,
        "period_type": full.period_type,
        "unit": full.unit,
        "currency": full.currency,
        "source_ids": full.source_ids,
        "derived_from_fact_ids": full.derived_from_fact_ids,
        "verification_status": full.verification_status.value,
    }
    for key, expected in checks.items():
        if key in core and core.get(key) != expected:
            raise ValueError(f"report_pack_core_fact_mismatch:{full.fact_id}:{key}")
    full_decimal = full.metadata.get("decimal_value", full.value)
    if _decimal(core.get("value")) != _decimal(full_decimal):
        raise ValueError(f"report_pack_core_fact_mismatch:{full.fact_id}:value")


def _fact_from_core(payload: Mapping[str, Any], ticker: str) -> FactRecord:
    if not payload.get("derived_from_fact_ids") or not payload.get("method_ref"):
        raise ValueError(f"report_pack_full_supplier_fact_missing:{payload.get('fact_id')}")
    available_at = payload.get("available_at")
    if not available_at:
        raise ValueError(f"report_pack_derived_available_at_missing:{payload.get('fact_id')}")
    return FactRecord(
        fact_id=str(payload["fact_id"]),
        ticker=ticker,
        metric_id=str(payload["metric_id"]),
        value=float(Decimal(str(payload["value"]))) if payload.get("value") is not None else None,
        unit=str(payload.get("unit") or ""),
        currency=str(payload.get("currency") or "CNY"),
        period_start=payload.get("period_start"),
        period_end=payload.get("period_end"),
        period_type=str(payload.get("period_type") or "instant"),
        as_of=available_at,
        scope=str(payload.get("scope") or "consolidated"),
        source_ids=list(payload.get("source_ids") or ()),
        verification_status=VerificationStatus(payload["verification_status"]),
        method_ref=str(payload["method_ref"]),
        derived_from_fact_ids=list(payload["derived_from_fact_ids"]),
        metadata={
            "available_at": str(available_at),
            "decimal_value": str(payload.get("value")),
            "nature": "deterministic",
            "report_bridge_version": REPORT_BRIDGE_VERSION,
            "source_namespace_id": payload.get("source_namespace_id"),
            "source_projection_role": payload.get("source_projection_role"),
        },
    )


def _selected_records(
    core: Mapping[str, Any],
    coverage: Mapping[str, Any],
    full_facts: Mapping[str, FactRecord],
    full_dimensions: Mapping[str, DimensionalFactRecord],
) -> tuple[list[FactRecord], list[DimensionalFactRecord], frozenset[str]]:
    core_index = _core_fact_index(core)
    seed_ids = {
        str(metric["fact"]["fact_id"])
        for metric in core.get("metrics", ())
        if metric.get("state") == "ready" and isinstance(metric.get("fact"), dict)
    }
    for requirement in coverage.get("requirements", ()):
        if requirement.get("state") == "ready":
            seed_ids.update(str(item) for item in requirement.get("fact_ids", ()))

    selected_dimensions = [
        full_dimensions[item]
        for item in sorted(seed_ids & set(full_dimensions))
    ]
    pending = list(sorted(seed_ids - set(full_dimensions)))
    selected: dict[str, FactRecord] = {}
    while pending:
        fact_id = pending.pop()
        if fact_id in selected:
            continue
        record = full_facts.get(fact_id)
        compact = core_index.get(fact_id)
        if record is not None and compact is not None:
            _assert_core_matches_full(compact, record)
        if record is None:
            if compact is None:
                raise ValueError(f"report_pack_selected_fact_missing:{fact_id}")
            record = _fact_from_core(compact, str(core["ticker"]))
        selected[fact_id] = record
        pending.extend(
            parent
            for parent in record.derived_from_fact_ids
            if parent not in selected
        )
    return (
        [selected[item] for item in sorted(selected)],
        selected_dimensions,
        frozenset(selected),
    )


def _candidate_sources(manifest: Mapping[str, Any]) -> dict[str, SourceRecord]:
    candidates: dict[str, SourceRecord] = {}
    paths: set[Path] = set()
    for source_input in manifest.get("source_inputs", ()):
        company_path = source_input.get("company_path")
        if company_path:
            paths.add(Path(str(company_path)).resolve() / "sources.jsonl")
        for item in source_input.get("manifests", ()):
            if item.get("path"):
                paths.add(Path(str(item["path"])).resolve().parent / "sources.jsonl")
    for path in sorted(paths):
        if not path.is_file():
            continue
        for payload in _read_jsonl(path):
            source = SourceRecord.model_validate(payload)
            current = candidates.get(source.source_id)
            if current is not None and current.model_dump(mode="json") != source.model_dump(mode="json"):
                raise ValueError(f"report_pack_conflicting_source:{source.source_id}")
            candidates[source.source_id] = source
    return candidates


def _reconstruct_source(
    source_id: str,
    facts: Iterable[FactRecord],
    dimensions: Iterable[DimensionalFactRecord],
) -> SourceRecord:
    records = [
        item
        for item in (*tuple(facts), *tuple(dimensions))
        if source_id in item.source_ids and item.structured_admission is not None
    ]
    if not records:
        raise ValueError(f"report_pack_source_missing:{source_id}")
    first = records[0]
    admission = first.structured_admission
    assert admission is not None
    metadata = first.metadata
    return SourceRecord(
        source_id=source_id,
        name=f"{metadata.get('source_definition_id', admission.source_policy_id)}结构化响应",
        source_type="supplier-structured",
        upstream_source_id=str(
            metadata.get("upstream_source_id")
            or metadata.get("source_definition_id")
            or admission.source_policy_id
        ),
        retrieved_at=admission.retrieved_at,
        document_hash=admission.snapshot_sha256,
        authority_level=3,
        source_definition_id=str(
            metadata.get("source_definition_id") or admission.source_policy_id
        ),
        source_definition_version=str(
            metadata.get("source_definition_version") or admission.source_policy_version
        ),
        raw_resource_snapshot_id=admission.raw_resource_snapshot_id,
        available_at=admission.available_at or admission.retrieved_at,
        notes="由冻结轻量包的完整准入事实重建来源投影",
        metadata={"report_bridge_version": REPORT_BRIDGE_VERSION},
    )


def _validate_source_binding(
    source: SourceRecord,
    facts: Iterable[FactRecord],
    dimensions: Iterable[DimensionalFactRecord],
) -> None:
    admissions = [
        item.structured_admission
        for item in (*tuple(facts), *tuple(dimensions))
        if source.source_id in item.source_ids and item.structured_admission is not None
    ]
    if not admissions:
        return
    snapshot_hashes = {item.snapshot_sha256 for item in admissions if item is not None}
    snapshot_ids = {item.raw_resource_snapshot_id for item in admissions if item is not None}
    if source.document_hash and source.document_hash not in snapshot_hashes:
        raise ValueError(f"report_pack_source_hash_mismatch:{source.source_id}")
    if source.raw_resource_snapshot_id and source.raw_resource_snapshot_id not in snapshot_ids:
        raise ValueError(f"report_pack_source_snapshot_mismatch:{source.source_id}")


def _sources(
    manifest: Mapping[str, Any],
    facts: list[FactRecord],
    dimensions: list[DimensionalFactRecord],
    source_lookup: Callable[[str], SourceRecord] | None,
) -> list[SourceRecord]:
    required = sorted(
        {
            source_id
            for item in (*facts, *dimensions)
            for source_id in item.source_ids
        }
    )
    candidates = _candidate_sources(manifest)
    result: list[SourceRecord] = []
    for source_id in required:
        source = candidates.get(source_id)
        if source is None and source_lookup is not None:
            try:
                source = source_lookup(source_id)
            except Exception:
                source = None
        if source is None:
            source = _reconstruct_source(source_id, facts, dimensions)
        _validate_source_binding(source, facts, dimensions)
        result.append(source)
    return result


def _alias_fact(fact: FactRecord, *, annual_periods: set[date]) -> FactRecord | None:
    target = METRIC_ALIASES.get(fact.metric_id)
    if target is None:
        return None
    period_type = (
        "annual"
        if fact.period_type == "cumulative" and fact.period_end in annual_periods
        else fact.period_type
    )
    identity = {
        "version": REPORT_BRIDGE_VERSION,
        "input_fact_id": fact.fact_id,
        "target_metric_id": target,
        "period_type": period_type,
    }
    return FactRecord(
        fact_id="report-alias-" + canonical_sha256(identity)[:32],
        ticker=fact.ticker,
        metric_id=target,
        value=fact.value,
        unit=fact.unit,
        currency=fact.currency,
        period_start=fact.period_start,
        period_end=fact.period_end,
        period_type=period_type,
        disclosed_at=fact.disclosed_at,
        as_of=fact.as_of,
        scope=fact.scope,
        audited=fact.audited,
        source_ids=list(fact.source_ids),
        verification_status=VerificationStatus.DERIVED,
        method_ref=f"{REPORT_BRIDGE_VERSION}:{fact.metric_id}->{target}",
        derived_from_fact_ids=[fact.fact_id],
        metadata={
            "available_at": fact.metadata.get("available_at", fact.as_of.isoformat()),
            "decimal_value": fact.metadata.get("decimal_value", str(fact.value)),
            "formula_version": REPORT_BRIDGE_VERSION,
            "formula_kind": "metric_alias_and_period_normalization",
            "input_fact_ids": [fact.fact_id],
            "original_metric_id": fact.metric_id,
            "original_period_type": fact.period_type,
            "nature": "deterministic",
        },
    )


def _coverage_projection(
    manifest: Mapping[str, Any], coverage: Mapping[str, Any]
) -> dict[str, Any]:
    questions = []
    by_step: dict[str, list[str]] = defaultdict(list)
    for item in coverage.get("question_routes", ()):
        question_id = str(item.get("question_id") or "unknown")
        step_id = question_id.split(".", 1)[0]
        quality_state = str(item.get("quality_state") or "pending")
        state = quality_state if quality_state in {"ready", "not_applicable"} else "pending"
        required = list(item.get("full_requirement_refs") or ())
        by_step[step_id].append(state)
        questions.append(
            {
                "step_id": step_id,
                "question_id": question_id,
                "title": item.get("title"),
                "state": state,
                "applicability": "applicable",
                "required_requirement_ids": required,
                "ready_requirement_ids": required if state == "ready" else [],
                "missing_requirement_ids": required if state == "pending" else [],
                "not_applicable_requirement_ids": required if state == "not_applicable" else [],
                "next_paths": [],
                "method_status": "skeleton",
                "assumption_status": "not_confirmed" if step_id in {"ES05", "ES08"} else "not_required",
                "analysis_status": "not_started",
                "scope_route": item.get("scope_route"),
                "activation_status": item.get("activation_status"),
                "quality_counts": item.get("quality_counts", {}),
            }
        )
    steps = [
        {
            "step_id": step_id,
            "state": "ready" if states and all(item == "ready" for item in states) else "pending",
        }
        for step_id, states in sorted(by_step.items())
    ]
    overall = "ready" if questions and all(item["state"] == "ready" for item in questions) else "pending"
    return {
        "overall_status": overall,
        "analysis_scope": manifest.get("periods", {}),
        "periods": manifest.get("periods", {}),
        "profile_id": manifest.get("profile_id"),
        "profile_sha256": manifest.get("profile_sha256"),
        "pack_id": manifest.get("pack_id"),
        "pack_identity_hash": manifest.get("pack_identity_hash"),
        "questions": questions,
        "steps": steps,
        "requirements": coverage.get("requirements", []),
        "counts": coverage.get("counts", {}),
        "question_quality_counts": coverage.get("question_quality_counts", {}),
        "full_coverage_preserved": bool(coverage.get("full_coverage_preserved")),
    }


def build_report_request(
    pack_dir: Path | str,
    *,
    industry: str | None = None,
    source_lookup: Callable[[str], SourceRecord] | None = None,
) -> ReportCreateRequest:
    """验证轻量包及输入指针，并构造同一冻结输入的报告请求。"""

    pack_path = Path(pack_dir).resolve()
    manifest, core, coverage = _validate_pack(pack_path)
    ticker = str(manifest["ticker"])
    full_facts, full_dimensions = _full_projection_records(manifest, ticker)
    selected, dimensions, selected_ids = _selected_records(
        core, coverage, full_facts, full_dimensions
    )
    annual_periods = {
        date.fromisoformat(item) for item in (manifest.get("periods") or {}).get("annual", ())
    }
    aliases = [
        alias
        for fact in selected
        if (alias := _alias_fact(fact, annual_periods=annual_periods)) is not None
    ]
    facts = sorted([*selected, *aliases], key=lambda item: item.fact_id)
    sources = _sources(manifest, selected, dimensions, source_lookup)

    as_of_date = date.fromisoformat(str(manifest["as_of"]))
    china_tz = timezone(timedelta(hours=8))
    as_of = datetime.combine(as_of_date, time.max, tzinfo=china_tz).astimezone(timezone.utc)
    market_facts = [
        item
        for item in selected
        if item.metric_id == "market_price"
        and item.value is not None
        and item.value > 0
        and is_fact_consumable(
            item,
            as_of=as_of,
            materialization_selected_ids=selected_ids,
        )
    ]
    price_fact = max(
        market_facts,
        key=lambda item: (item.period_end or item.as_of.date(), item.as_of, item.fact_id),
        default=None,
    )

    profile = load_research_profile(str(manifest["profile_id"]))
    supported = profile.get("industry_profile", {}).get("supported_tickers", {})
    resolved_industry = industry or ("消费" if ticker in supported else "未知")
    coverage_hash = str(manifest["output_hashes"]["core-coverage.json"])
    return ReportCreateRequest(
        ticker=ticker,
        company_name=str(core.get("company_name") or supported.get(ticker) or ticker),
        industry=resolved_industry,
        as_of=as_of,
        current_price=price_fact.value if price_fact else None,
        price_as_of=price_fact.as_of if price_fact else None,
        sources=sources,
        facts=facts,
        dimensional_facts=dimensions,
        use_synced_facts=False,
        research_coverage_snapshot_id="lite-coverage-" + coverage_hash[:24],
        research_coverage=_coverage_projection(manifest, coverage),
        materialization_selected_fact_ids=sorted(selected_ids),
        report_notes=(
            f"由冻结轻量包 {manifest['pack_id']} 生成；原文片段未自动转换为分析结论；"
            "未提供用户确认的预测假设，估值和评级保持待补。"
        ),
        input_metadata={
            "report_bridge_version": REPORT_BRIDGE_VERSION,
            "lite_pack_id": manifest["pack_id"],
            "lite_pack_identity_hash": manifest["pack_identity_hash"],
            "lite_pack_version": manifest["pack_version"],
            "lite_profile_id": manifest["profile_id"],
            "lite_profile_sha256": manifest["profile_sha256"],
            "lite_pack_path": str(pack_path),
        },
    )


__all__ = [
    "METRIC_ALIASES",
    "REPORT_BRIDGE_VERSION",
    "build_report_request",
]
