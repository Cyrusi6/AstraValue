"""从当前研究输入生成逐题资料覆盖；不推断研究判断已经完成。"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .registry import canonical_registry_sha256
from .scope import ROOT, load_scope


COVERAGE_VERSION = "research-question-coverage-v1.0.0"
_EMPTY_VALUES = {"", "-", "--", "—", "…", "N/A", "NA", "null"}
_CALCULATION_METRICS = {
    "K02": {"gross_margin", "net_margin", "parent_net_margin", "selling_expense_ratio",
            "administrative_expense_ratio", "rd_ratio", "finance_expense_ratio"},
    "K04": {"cash_profit_ratio"},
    "K06": {"operating_cash_flow_less_asset_purchase_proxy", "capex_to_revenue",
            "operating_cash_flow_margin"},
}


def _load_registry() -> dict[str, Any]:
    path = ROOT / "config/structured_data/research_requirements.v1.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    if canonical_registry_sha256(registry) != registry.get("content_sha256"):
        raise ValueError("research_requirements_hash_mismatch")
    return registry


def _field_periods() -> tuple[dict[tuple[str, str], str], str]:
    registry = json.loads((ROOT / "config/structured_data/fields.v1.json").read_text(encoding="utf-8"))
    if canonical_registry_sha256(registry) != registry.get("content_sha256"):
        raise ValueError("research_fields_hash_mismatch")
    return ({(item["dataset_id"], item["raw_name"]): item["period_semantics"] for item in registry["fields"]},
            registry["content_sha256"])


def _present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and value.strip() in _EMPTY_VALUES)


def _source_text(value: Any) -> bool:
    if not isinstance(value, str) or not _present(value):
        return False
    try:
        Decimal(value)
        return False
    except InvalidOperation:
        return True


def _available_at(item: Mapping[str, Any]) -> datetime | None:
    metadata = item.get("metadata") or {}
    raw = metadata.get("available_at") or item.get("available_at") or item.get("as_of")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _available(item: Mapping[str, Any], cutoff: datetime, *, optional: bool = False) -> bool:
    value = _available_at(item)
    if value is None:
        return optional and not ((item.get("metadata") or {}).get("available_at")
                                 or item.get("available_at") or item.get("as_of"))
    return value <= cutoff


def _select_field_facts(items: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], bool]:
    """保留独立维度，仅显式且有序的修订可替换同一来源行。"""
    grouped = defaultdict(list)
    for item in items:
        grouped[tuple(item.get(key) for key in (
            "metric_id", "period_type", "scope", "currency", "dimension_type", "dimension_code",
            "dimension_name", "parent_dimension", "accounting_basis"))].append(item)
    selected = []
    for values in grouped.values():
        by_record = {(item.get("metadata") or {}).get("structured_record_version_id"): item
                     for item in values if (item.get("metadata") or {}).get("structured_record_version_id")}
        superseded = set()
        for item in values:
            metadata = item.get("metadata") or {}
            parent_id = metadata.get("supersedes_record_version_id")
            parent = by_record.get(parent_id)
            if parent:
                previous = parent.get("metadata") or {}
                keys = ("storage_namespace_id", "source_definition_id", "source_definition_version", "structured_row_key")
                if (not metadata.get("structured_row_key") or any(metadata.get(key) != previous.get(key) for key in keys)
                        or _available_at(parent) >= _available_at(item)):
                    return [], True
                superseded.add(parent_id)
        leaves = [item for item in values if (item.get("metadata") or {}).get("structured_record_version_id") not in superseded]
        amounts = {(str(Decimal(str((item.get("metadata") or {}).get("decimal_value", item["value"]))).normalize()), item.get("unit"))
                   for item in leaves}
        if not leaves or len(amounts) > 1:
            return [], True
        selected.extend(leaves)
    return selected, False


def _periods(mode: str, periods: Mapping[str, Any], as_of: date) -> list[str]:
    """保留注册表 FIN/BIZ/EVT/IND 优先级，窗口取本次 profile。"""
    annual = list(periods["annual"])
    quarters = list(periods["quarters"])
    if "FIN" in mode:
        return sorted(set(annual + quarters))
    if "BIZ" in mode:
        year = as_of.year if as_of >= date(as_of.year, 8, 31) else as_of.year - 1
        return sorted(set(annual + [f"{year - 1}-06-30", f"{year}-06-30"]))
    if "EVT" in mode or "IND" in mode:
        return [f"{annual[0][:4]}-01-01/{as_of.isoformat()}"]
    return [as_of.isoformat()]


def _matches_period(value: str | None, required: str) -> bool:
    if not value:
        return False
    if "/" in required:
        start, end = required.split("/", 1)
        return start <= value <= end
    return value == required


def _active_path(path: Mapping[str, Any], scope: Mapping[str, Any], profile: Mapping[str, Any]) -> bool:
    dataset, raw = path.get("dataset_id"), path.get("raw_name")
    if not dataset:
        return True
    rule = scope["datasets"].get(dataset)
    selected = profile["datasets"].get(dataset)
    return bool(rule and rule["selection"] != "excluded" and selected
                and (not raw or raw in rule["consume_fields"] and raw in selected["fields"]))


def build_question_coverage(
    *,
    ticker: str,
    as_of: date,
    facts: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    periods: Mapping[str, Any],
) -> dict[str, Any]:
    """接收已选事实、已准入记录和有定位证据，返回冻结行、工作项及摘要。

    ready 仅表示该字段要求已有正式数值。原始文本、事件集合、计算路线及
    研究上下文分别保留阅读或处理状态，不能据此宣布整题分析完成。
    """
    if periods.get("current") != as_of.isoformat() or not periods.get("annual"):
        raise ValueError("research_coverage_periods_mismatch")
    registry, scope = _load_registry(), load_scope()
    field_periods, fields_sha256 = _field_periods()
    route_for = {qid: route for route, ids in profile["question_routing"].items() for qid in ids}
    question_ids = {row["question_id"] for row in registry["questions"]}
    if set(route_for) != question_ids:
        raise ValueError("research_coverage_question_routing_mismatch")
    cutoff = datetime.combine(as_of, time.max, timezone(timedelta(hours=8)))
    fact_index: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    period_mismatches: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    fact_conflicts: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    selected_facts = []
    for fact in facts:
        if fact.get("ticker") != ticker:
            raise ValueError("research_coverage_fact_company_mismatch")
        period = str(fact.get("period_end") or "")
        if not period or period > as_of.isoformat() or not _available(fact, cutoff):
            continue
        selected_facts.append(fact)
        metadata = fact.get("metadata") or {}
        dataset = metadata.get("structured_dataset_id")
        field = str(metadata.get("structured_field_path") or "").removeprefix("$.")
        if dataset and field and fact.get("value") is not None and not fact.get("derived_from_fact_ids"):
            expected = {"cumulative_or_annual": {"cumulative", "annual"}, "single_quarter": {"single_quarter"},
                        "point_in_time": {"instant"}, "daily": {"market_quote", "instant"}}.get(field_periods.get((dataset, field)))
            if expected and fact.get("period_type") not in expected:
                period_mismatches[(dataset, field, period)].append(fact)
            else:
                fact_index[(dataset, field, period)].append(fact)
    for key, values in list(fact_index.items()):
        selected, conflict = _select_field_facts(values)
        if conflict:
            fact_conflicts[key] = values
        fact_index[key] = selected

    record_index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("company") != ticker:
            raise ValueError("research_coverage_record_company_mismatch")
        if _available(record, cutoff) and str(record.get("period") or "") <= as_of.isoformat():
            record_index[str(record["dataset_id"])].append(record)
    for values in record_index.values():
        values.sort(key=lambda item: str(item["record_id"]))
    text_index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in evidence:
        if item.get("company") != ticker:
            raise ValueError("research_coverage_evidence_company_mismatch")
        if _available(item, cutoff, optional=True):
            text_index[str(item.get("route_id") or "")].append(item)

    calculation_routes = {item["route_id"]: item for item in registry["calculation_routes"]}
    provenance = {
        "coverage_version": COVERAGE_VERSION,
        "registry_id": registry["registry_id"],
        "registry_sha256": registry["content_sha256"],
        "field_registry_sha256": fields_sha256,
        "scope_sha256": scope["content_sha256"],
        "profile_id": profile["profile_id"],
        "profile_sha256": profile["content_sha256"],
    }
    rows, work = [], []
    for requirement in registry["requirements"]:
        qid, rid = requirement["question_id"], requirement["requirement_id"]
        override = scope.get("requirement_overrides", {}).get(rid, {})
        for registered_path in requirement["paths"]:
            path = dict(registered_path)
            if "dataset_id" in override:
                path.update(dataset_id=override["dataset_id"], raw_name=override["raw_name"],
                            scope_basis=override["basis"])
            active = _active_path(path, scope, profile)
            dataset, raw, kind = path.get("dataset_id"), path.get("raw_name"), path["kind"]
            for period in _periods(path["period_semantics"], periods, as_of):
                effective_period = period
                if dataset == "market_cap" and "NOW" in path["period_semantics"] and raw:
                    available = sorted(p for d, f, p in fact_index if d == dataset and f == raw
                                       and p <= as_of.isoformat())
                    if available and (as_of - date.fromisoformat(available[-1])).days <= 14:
                        effective_period = available[-1]
                direct = list(fact_index.get((dataset, raw, effective_period), ())) if active else []
                conflicts = fact_conflicts.get((dataset, raw, effective_period), []) if active else []
                mismatches = period_mismatches.get((dataset, raw, effective_period), []) if active else []
                dataset_records = [item for item in record_index.get(dataset, ())
                                   if not raw or raw in (item.get("fields") or {})]
                observed = [item for item in dataset_records
                            if _matches_period(item.get("period"), effective_period)] if active else []
                current_context = dataset == "company_basic" and "NOW" in path["period_semantics"]
                if current_context and active:
                    observed = list(dataset_records)
                    effective_period = None
                undated = False
                if not observed and active:
                    observed = [item for item in dataset_records if not item.get("period")]
                    undated = bool(observed)
                observations = [
                    {"record_id": item["record_id"], "snapshot_id": item.get("snapshot_id"),
                     "row_key": item.get("row_key"), "period": item.get("period"),
                     "available_at": item.get("available_at"), "field_path": "$." + raw,
                     "source_ids": list(item.get("source_ids") or ()), "value": item["fields"][raw]}
                    for item in observed if raw and raw in (item.get("fields") or {})
                ]
                text = [item for item in text_index.get(path.get("route_id"), ())
                        if _matches_period(item.get("period"), period)]
                fact_ids = sorted({str(f.get("fact_id") or f["dimensional_fact_id"]) for f in direct})
                record_ids = sorted({str(item["record_id"]) for item in observed
                                     if kind == "record_set" or raw and raw in (item.get("fields") or {})})
                state, reason, stage = "pending", "input_not_acquired", "acquisition"
                calculation_status = None
                if kind == "raw_field":
                    if fact_ids:
                        state, reason = "ready", None
                    elif conflicts:
                        reason, stage = "source_or_revision_conflict", "semantic_processing"
                    elif mismatches:
                        reason, stage = "fact_period_type_mismatch", "semantic_processing"
                    elif observations:
                        stage = "semantic_processing"
                        reason = "observed_field_period_unconfirmed" if undated else "semantic_definition_or_period_unconfirmed"
                        if not any(_present(o["value"]) for o in observations):
                            reason = "observed_value_missing"
                        if not undated and raw not in {"TYPE", "TYPE_CODE"} and all(_source_text(o["value"]) for o in observations):
                            state, reason = "source_text_available", "source_statement_requires_question_review"
                elif kind == "reading_section":
                    stage, reason = "document_reading", "document_semantic_evidence_pending"
                    if text:
                        state, reason = "source_text_available", "source_passages_organized_question_review_pending"
                elif kind == "record_set":
                    stage = "semantic_processing" if record_ids else "acquisition"
                    reason = "record_set_available_lifecycle_or_field_semantics_pending" if record_ids else "input_not_acquired"
                elif kind == "calculation":
                    route = calculation_routes[path["route_id"]]
                    missing = []
                    for ref in route["input_refs"]:
                        if not ref.startswith("f:"):
                            missing.append(ref)
                        else:
                            input_dataset, input_field = ref[2:].split(".", 1)
                            if not fact_index.get((input_dataset, input_field, period)):
                                missing.append(ref)
                    computed = [f for f in selected_facts if str(f.get("period_end")) == period
                                and (f.get("metric_id") in _CALCULATION_METRICS.get(route["route_id"], set())
                                     or route["route_id"] == "K01" and f.get("derived_from_fact_ids")
                                     and f.get("period_type") in {"single_quarter", "ttm"})]
                    calculation_status = {
                        "route_id": route["route_id"], "missing_verified_inputs": missing,
                        "boundary": route["formula_boundary"], "complete_route_integrated": False,
                        "available_computed_fact_ids": sorted({str(f["fact_id"]) for f in computed}),
                    }
                    stage = "deterministic_calculation"
                    reason = "calculation_verified_inputs_pending" if missing else "calculation_route_integration_pending"
                else:
                    stage = "research_context"
                    reason = "registered_source_or_definition_gap" if kind == "gap" else "research_context_pending"
                if not active:
                    state, reason, stage = "pending", "outside_research_profile", "research_context"
                if override.get("scope_disposition") == "not_required_by_default":
                    state, reason, stage = "not_applicable", "versioned_scope_does_not_require_input", "research_context"
                text_ids = sorted({str(item["evidence_id"]) for item in text})
                next_action = (None if state in {"ready", "not_applicable"} else
                               "semantic_review" if observations or text_ids or record_ids else
                               "acquire_exact_input" if stage == "acquisition" else "prepare_" + kind)
                row = {
                    **provenance, "company": ticker, "question_id": qid, "requirement_id": rid,
                    "requiredness": requirement["requiredness"], "combination": requirement["combination"],
                    "applicability_condition": requirement["applicability_condition"],
                    "period": period, "effective_input_period": effective_period, "input": path,
                    "scope_override": dict(override), "scope_route": route_for[qid],
                    "active_in_profile": active,
                    "required_for_active_coverage": active and route_for[qid] == "core" and requirement["requiredness"] == "required",
                    "state": state, "reason": reason, "fact_ids": fact_ids,
                    "conflicting_fact_ids": sorted({str(f.get("fact_id") or f["dimensional_fact_id"]) for f in conflicts}),
                    "period_mismatch_fact_ids": sorted({str(f.get("fact_id") or f["dimensional_fact_id"]) for f in mismatches}),
                    "record_ids": record_ids, "observations": observations[:8],
                    "observation_count": len(observations), "text_evidence_ids": text_ids,
                    "source_ids": sorted({str(s) for item in direct + conflicts + mismatches + observed + text for s in item.get("source_ids", ())}),
                    "calculation_status": calculation_status, "next_action": next_action,
                    "analysis_status": "not_started",
                }
                rows.append(row)
                if state not in {"ready", "not_applicable"} and route_for[qid] == "core" and active:
                    work.append({
                        "company": ticker, "question_id": qid, "requirement_id": rid, "period": period,
                        "stage": stage, "dataset_id": dataset, "raw_name": raw, "route_id": path.get("route_id"),
                        "reason": reason, "acquire_allowed": stage == "acquisition" and reason == "input_not_acquired",
                        "record_ids": record_ids, "evidence_ids": text_ids, "profile_id": profile["profile_id"],
                        "trigger": "active_lite_core_requirement",
                    })
    question_counts = {qid: dict(Counter(row["state"] for row in rows if row["question_id"] == qid))
                       for qid in sorted(question_ids)}
    return {
        "rows": rows, "work_items": work,
        "summary": {**provenance, "company": ticker, "as_of": as_of.isoformat(),
                    "question_count": len(question_ids), "requirement_count": len(registry["requirements"]),
                    "period_inputs": len(rows), "states": dict(Counter(row["state"] for row in rows)),
                    "required_active_states": dict(Counter(row["state"] for row in rows if row["required_for_active_coverage"])),
                    "required_active_question_counts": {qid: dict(Counter(row["state"] for row in rows
                        if row["question_id"] == qid and row["required_for_active_coverage"])) for qid in sorted(question_ids)},
                    "question_counts": question_counts, "full_coverage_preserved": True,
                    "analysis_status": "not_started"},
    }
