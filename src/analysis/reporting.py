from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .formulas import (
    compute_financial_snapshot,
    normalize_financial_facts,
    reconcile_balance_sheet,
    reconcile_cash_flow,
)
from .event_state import current_event_versions, load_event_taxonomy
from .industry import IndustryRouter
from .models import (
    AuditAppendix,
    ClaimRecord,
    ConclusionCard,
    DimensionalFactRecord,
    EventRecord,
    FactRecord,
    MethodBundle,
    ModelRun,
    ModelRunStatus,
    ReportCreateRequest,
    ReportSection,
    ReportVersion,
    ResearchRating,
    TrackingIndicator,
    VerificationStatus,
)
from .policies import load_report_policy, load_source_policy
from .registry import MethodRegistry
from .scenarios import build_scenario_projections
from .valuation import execute_valuation
from .verification import are_independent, evidence_scores, validate_claims
from .structured.consumption import is_fact_consumable, latest_consumable_facts


STEP_METHOD_IDS = [
    "STEP.BUSINESS",
    "STEP.FINANCIAL",
    "STEP.GOVERNANCE",
    "STEP.CAPITAL",
    "STEP.VALUATION",
    "STEP.DRIVERS_RISKS",
    "STEP.INDUSTRY",
    "STEP.SCENARIOS",
]

FINANCIAL_METHOD_IDS = [
    "FIN.NORMALIZATION",
    "FIN.INCOME",
    "FIN.BALANCE",
    "FIN.CASHFLOW",
    "FIN.DUPONT",
    "FIN.ROIC_WACC",
    "FIN.FCF_WORKING_CAPITAL",
    "FIN.EARNINGS_QUALITY",
]

STEP_TITLES = [
    "公司业务与商业模式",
    "财务报表分析",
    "公司治理与管理层",
    "资本行为",
    "估值分析",
    "核心驱动因素与潜在风险",
    "行业情况",
    "情景假设与结论",
]

CATEGORY_MAP = {
    1: {"business", "moat", "strategy", "capacity", "customer", "supplier"},
    2: {"financial", "income", "balance", "cashflow", "quality"},
    3: {"governance", "management", "audit", "control"},
    4: {"capital", "dividend", "buyback", "financing", "m&a"},
    5: {"valuation"},
    6: {"driver", "risk", "catalyst", "invalidation"},
    7: {"industry", "competition", "supply", "demand"},
    8: {"scenario", "tracking", "conclusion"},
}

DIMENSION_TYPE_NAMES = {
    "business": "主营业务",
    "product": "产品",
    "region": "地区",
    "channel": "销售渠道",
    "capacity": "产能",
    "customer": "客户集中度",
    "supplier": "供应商集中度",
}


class ReportBuildError(ValueError):
    pass


class ReportBuilder:
    def __init__(self, registry: MethodRegistry | None = None) -> None:
        self.registry = registry or MethodRegistry()
        self.router = IndustryRouter(self.registry)
        self.policy = load_report_policy(str(self.registry.config_dir))
        self.source_policy = load_source_policy(str(self.registry.config_dir))
        self.event_taxonomy = load_event_taxonomy()

    def build(
        self,
        request: ReportCreateRequest,
        *,
        version: int = 1,
        parent_report_id: str | None = None,
        bundle_override: MethodBundle | None = None,
        version_changes: list[str] | None = None,
    ) -> ReportVersion:
        # Re-validate a copied payload so callers cannot bypass model invariants
        # by mutating an already-created Pydantic object before building.
        request = ReportCreateRequest.model_validate(request.model_dump(mode="python"))
        materialization_selected_ids = (
            frozenset(request.materialization_selected_fact_ids)
            if request.materialization_selected_fact_ids
            else None
        )
        claims_errors = validate_claims(request.claims, request.facts, request.sources)
        input_errors = _validate_request_lineage(request)
        if claims_errors or input_errors:
            raise ReportBuildError("；".join([*claims_errors, *input_errors]))

        facts = normalize_financial_facts(self._point_in_time_facts(request.facts, request.as_of))
        dimensional_facts = [
            item
            for item in request.dimensional_facts
            if _datetime_le(item.available_at, request.as_of)
        ]
        events = [
            item
            for item in request.events
            if _datetime_le(item.available_at, request.as_of)
        ]
        claims = [claim for claim in request.claims if _datetime_le(claim.as_of, request.as_of)]
        sources = self._point_in_time_sources(
            request,
            facts,
            claims,
            dimensional_facts,
            events,
        )
        point_in_time_errors = _validate_point_in_time_lineage(
            request,
            facts,
            claims,
            sources,
            dimensional_facts,
            events,
        )
        if point_in_time_errors:
            raise ReportBuildError("；".join(point_in_time_errors))
        route = self.router.resolve(request.industry)
        method_ids = STEP_METHOD_IDS + FINANCIAL_METHOD_IDS + [route.industry_method_id, *route.valuation_method_ids]
        bundle = bundle_override or self.registry.create_bundle(method_ids)
        method_refs = {method.method_id: method.ref for method in bundle.methods}
        missing_bundle_methods = sorted(set(method_ids) - set(method_refs))
        if missing_bundle_methods:
            raise ReportBuildError(f"冻结方法集合缺少当前报告所需方法: {', '.join(missing_bundle_methods)}")
        data_snapshot_id = _snapshot_id(
            request,
            facts,
            sources,
            claims,
            dimensional_facts,
            events,
        )

        latest = _latest_numeric_facts(
            facts, materialization_selected_ids=materialization_selected_ids
        )
        derived = compute_financial_snapshot(latest)
        conflicts = [f"{fact.metric_id}: 核验状态为待核验" for fact in facts if fact.verification_status == VerificationStatus.PENDING]
        conflicts.extend(
            f"{item.metric_id}/{item.dimension_name}: 经营维度核验状态为待核验"
            for item in dimensional_facts
            if item.verification_status == VerificationStatus.PENDING
        )
        conflicts.extend(
            (
                f"{item.event_type}/{item.summary}"
                f" [event_id={item.event_id};"
                f" period_end={item.period_end.isoformat() if item.period_end else 'unknown'}]:"
                " 事件核验状态为待核验"
            )
            for item in events
            if item.verification_status == VerificationStatus.PENDING
        )
        missing = [
            f"{fact.metric_id}: {fact.verification_status.value}"
            for fact in facts
            if fact.verification_status in {VerificationStatus.UNAVAILABLE, VerificationStatus.NOT_DISCLOSED}
        ]
        missing.extend(
            f"{item.metric_id}/{item.dimension_name}: {item.verification_status.value}"
            for item in dimensional_facts
            if item.verification_status
            in {VerificationStatus.UNAVAILABLE, VerificationStatus.NOT_DISCLOSED}
        )
        missing.extend(
            f"{item.event_type}/{item.summary}: {item.verification_status.value}"
            for item in events
            if item.verification_status
            in {VerificationStatus.UNAVAILABLE, VerificationStatus.NOT_DISCLOSED}
        )
        for metric_id in route.required_metrics:
            if metric_id not in latest and derived.get(metric_id) is None:
                missing.append(f"{metric_id}: 行业模型必需指标缺失")
        critical_facts = self._critical_facts_in_analysis_window(facts)
        for metric_id in self.source_policy.critical_metrics:
            metric_facts = [
                item
                for item in critical_facts
                if item.metric_id == metric_id and item.value is not None
            ]
            if metric_facts and not all(
                is_fact_consumable(
                    item,
                    materialization_selected_ids=materialization_selected_ids,
                )
                for item in metric_facts
            ):
                missing.append(f"{metric_id}: 尚有期间未通过统一事实准入")

        selected_methods = route.valuation_method_ids
        if request.input_metadata.get("agent_research"):
            if set(request.model_inputs) - set(selected_methods):
                raise ValueError("agent_valuation_method_not_applicable_to_industry")
            selected_methods = tuple(m for m in selected_methods if m in request.model_inputs)
        valuation_results, model_runs, unreferenced_numbers = self._run_valuations(
            selected_methods,
            bundle,
            request.model_inputs,
            conflicts,
            facts,
            request.assumptions,
            sources,
        )
        scenarios = build_scenario_projections(
            request.assumptions,
            method_ref=method_refs["STEP.SCENARIOS"],
            as_of=request.as_of,
        )
        data_quality_checks = self._data_quality_checks(
            facts,
            latest,
            method_refs,
            research_coverage=request.research_coverage,
            materialization_selected_ids=materialization_selected_ids,
        )
        sections = self._sections(
            facts,
            dimensional_facts,
            events,
            claims,
            latest,
            derived,
            valuation_results,
            model_runs,
            scenarios,
            method_refs,
            route.warnings,
            data_quality_checks,
        )
        completeness, confidence = evidence_scores(facts, claims)
        questions = request.research_coverage.get("questions", [])
        if questions:
            applicable = [q for q in questions if q.get("state") != "not_applicable"]
            ready_count = sum(q.get("state") == "ready" for q in applicable)
            completeness = min(completeness, ready_count / len(applicable)) if applicable else completeness
            for q in applicable:
                for rid in q.get("missing_requirement_ids", []):
                    missing.append(f"{q['question_id']}/{rid}: 完整研究要求待补")
                if q.get("state") != "ready" and not q.get("missing_requirement_ids"):
                    missing.append(f"{q['question_id']}: 问题仍待补")
        if request.research_coverage and not claims:
            completeness = 0.0
            missing.append("分析对象：尚无带证据的 ClaimRecord")
        missing.extend(request.input_metadata.get("semantic_gaps", []))
        missing.extend(f"{run.method_ref}: {run.failure_reason}" for run in model_runs if run.failure_reason)
        from .structured.report_semantics import apply_sections
        apply_sections(request, sections)
        conclusion = self._conclusion(request, claims, valuation_results, scenarios, conflicts, completeness, confidence)
        tracking_indicators = _tracking_indicators(request)
        active_fact_ids = {fact.fact_id for fact in facts}
        active_verification_fact_ids = set(active_fact_ids)
        for fact in facts:
            active_verification_fact_ids.update(
                str(item) for item in fact.metadata.get("consolidated_from_fact_ids", [])
            )
        peer_sets = [
            item for item in request.peer_sets
            if _datetime_le(item.as_of, request.as_of)
        ]
        audit = AuditAppendix(
            sources=sources,
            method_bundle=bundle,
            assumptions=request.assumptions,
            conflicts=conflicts,
            missing_items=sorted(set(missing)),
            version_changes=version_changes or [],
            model_runs=model_runs,
            verification_records=[
                item
                for item in request.verification_records
                if item.left_fact_id in active_verification_fact_ids
                or item.right_fact_id in active_verification_fact_ids
            ],
            data_quality_checks=data_quality_checks,
            unreferenced_numbers=unreferenced_numbers,
            dimensional_fact_ids=[
                item.dimensional_fact_id for item in dimensional_facts
            ],
            event_ids=[item.event_id for item in events],
            peer_set_refs=[item.peer_set_id for item in peer_sets],
        )
        report = ReportVersion(
            parent_report_id=parent_report_id,
            version=version,
            ticker=request.ticker,
            company_name=request.company_name,
            industry=request.industry,
            as_of=request.as_of,
            data_snapshot_id=data_snapshot_id,
            method_bundle_id=bundle.method_bundle_id,
            conclusion=conclusion,
            sections=sections,
            audit=audit,
            facts=facts,
            dimensional_facts=dimensional_facts,
            events=events,
            industry_facts=[f for f in request.industry_facts if _datetime_le(f.available_at, request.as_of)],
            peer_sets=peer_sets,
            claims=claims,
            assumptions=request.assumptions,
            tracking_indicators=tracking_indicators,
            model_inputs=request.model_inputs,
            request_metadata={
                **dict(request.input_metadata),
                "industry_route": route.key,
                "report_notes": request.report_notes,
                "sync_result_id": request.sync_result_id,
                "dimensional_sync_result_id": request.dimensional_sync_result_id,
                "event_sync_result_id": request.event_sync_result_id,
            },
            research_coverage_snapshot_id=request.research_coverage_snapshot_id,
            research_coverage=dict(request.research_coverage),
            materialization_selected_fact_ids=sorted(
                request.materialization_selected_fact_ids
            ),
        )

        if request.input_metadata.get("agent_research"):
            from .research.reports import apply_agent_research
            report = apply_agent_research(report, request)
        return report

    @staticmethod
    def _point_in_time_facts(facts: list[FactRecord], as_of: datetime) -> list[FactRecord]:
        return [
            fact
            for fact in facts
            if _datetime_le(fact.as_of, as_of) and (fact.disclosed_at is None or _datetime_le(fact.disclosed_at, as_of))
        ]

    @staticmethod
    def _point_in_time_sources(
        request: ReportCreateRequest,
        facts: list[FactRecord],
        claims: list[ClaimRecord],
        dimensional_facts: list,
        events: list[EventRecord],
    ) -> list:
        referenced = {
            source_id
            for fact in facts
            for source_id in fact.source_ids
        } | {
            source_id
            for claim in claims
            for source_id in claim.evidence_source_ids
        }
        referenced.update(
            source_id
            for item in dimensional_facts
            for source_id in item.source_ids
        )
        referenced.update(
            source_id
            for item in events
            for source_id in item.source_ids
        )
        referenced.update(
            source_id
            for assumption in request.assumptions
            for source_id in assumption.source_ids
        )
        for inputs in request.model_inputs.values():
            for refs in _normalize_lineage(inputs.get("_lineage", {})).values():
                referenced.update(ref.split(":", 1)[1] for ref in refs if ref.startswith("source:"))
        referenced.update(sid for item in [*request.industry_facts, *request.peer_sets] for sid in item.source_ids)
        # Peer metric rows carry their own frozen structured source IDs. Keep
        # those sources in the report audit appendix as well as the synthetic
        # peer-projection source that identifies the input files.
        for peer_set in request.peer_sets:
            for peer in (peer_set.metadata or {}).get("peers", []):
                for metric in peer.get("metrics", []):
                    referenced.update(metric.get("source_ids", []))
        return [
            source
            for source in request.sources
            if (source.source_id in referenced or not referenced)
            and (source.published_at is None or _datetime_le(source.published_at, request.as_of))
        ]

    def _run_valuations(
        self,
        method_ids: tuple[str, ...],
        bundle: MethodBundle,
        model_inputs: dict[str, dict[str, Any]],
        conflicts: list[str],
        facts: list[FactRecord],
        assumptions: list,
        sources: list,
    ) -> tuple[list, list[ModelRun], list[str]]:
        specs = {item.method_id: item for item in bundle.methods}
        results = []
        runs = []
        unreferenced_numbers: list[str] = []
        valid_refs = {
            *(f"fact:{item.fact_id}" for item in facts),
            *(f"assumption:{item.assumption_id}" for item in assumptions),
            *(f"source:{item.source_id}" for item in sources),
        }
        for method_id in method_ids:
            spec = specs[method_id]
            inputs = model_inputs.get(method_id)
            if conflicts and inputs and (inputs.get("_uses_conflicted_facts") or inputs.get("uses_conflicted_facts")):
                runs.append(ModelRun(method_ref=spec.ref, status=ModelRunStatus.BLOCKED, inputs=inputs, failure_reason="关键输入待核验"))
                continue
            if not inputs:
                runs.append(ModelRun(method_ref=spec.ref, status=ModelRunStatus.BLOCKED, failure_reason="没有提供模型输入"))
                continue
            clean_inputs = {key: value for key, value in inputs.items() if not key.startswith("_") and key != "uses_conflicted_facts"}
            input_lineage = _normalize_lineage(inputs.get("_lineage", {}))
            numeric_keys = _numeric_input_keys(clean_inputs)
            missing_lineage = sorted(key for key in numeric_keys if not input_lineage.get(key))
            invalid_refs = sorted(
                ref
                for refs in input_lineage.values()
                for ref in refs
                if ref not in valid_refs and not ref.startswith("formula:")
            )
            if missing_lineage or invalid_refs:
                reasons = []
                if missing_lineage:
                    reasons.append(f"数值输入缺少血缘: {', '.join(missing_lineage)}")
                    unreferenced_numbers.extend(f"{method_id}.{key}" for key in missing_lineage)
                if invalid_refs:
                    reasons.append(f"输入引用不存在: {', '.join(invalid_refs)}")
                runs.append(
                    ModelRun(
                        method_ref=spec.ref,
                        status=ModelRunStatus.BLOCKED,
                        inputs=clean_inputs,
                        input_lineage=input_lineage,
                        failure_reason="；".join(reasons),
                    )
                )
                continue
            result = execute_valuation(method_id, spec.version, clean_inputs)
            results.append(result)
            fact_ids = sorted({ref.split(":", 1)[1] for refs in input_lineage.values() for ref in refs if ref.startswith("fact:")})
            assumption_ids = sorted({ref.split(":", 1)[1] for refs in input_lineage.values() for ref in refs if ref.startswith("assumption:")})
            runs.append(
                ModelRun(
                    method_ref=spec.ref,
                    status=result.status,
                    inputs=clean_inputs,
                    input_lineage=input_lineage,
                    input_fact_ids=fact_ids,
                    input_assumption_ids=assumption_ids,
                    outputs=result.model_dump(mode="json"),
                    failure_reason=result.failure_reason,
                )
            )
        return results, runs, sorted(set(unreferenced_numbers))

    def _sections(
        self,
        facts: list[FactRecord],
        dimensional_facts: list,
        events: list[EventRecord],
        claims: list[ClaimRecord],
        latest: dict[str, float],
        derived: dict[str, float | None],
        valuations: list,
        model_runs: list[ModelRun],
        scenarios: list[dict[str, Any]],
        method_refs: dict[str, str],
        route_warnings: tuple[str, ...],
        data_quality_checks: list[dict[str, Any]],
    ) -> list[ReportSection]:
        result = []
        for number, (title, step_method) in enumerate(zip(STEP_TITLES, STEP_METHOD_IDS), 1):
            section_claims = [claim for claim in claims if claim.category.lower() in CATEGORY_MAP[number]]
            section = ReportSection(
                number=number,
                title=title,
                summary=_section_summary(number, section_claims),
                claims=section_claims,
                method_refs=[method_refs[step_method]],
            )
            if number == 1 and dimensional_facts:
                dimension_types = sorted(
                    {
                        DIMENSION_TYPE_NAMES.get(
                            item.dimension_type,
                            item.dimension_type,
                        )
                        for item in dimensional_facts
                    }
                )
                if not section_claims:
                    section.summary = (
                        f"已载入{len(dimensional_facts)}条可追溯经营维度事实，"
                        f"覆盖{', '.join(dimension_types)}；"
                        "商业模式与护城河判断仍需证据化结论。"
                    )
                section.tables = [
                    {
                        "name": "经营维度事实",
                        "rows": [
                            _format_dimensional_fact(item)
                            for item in dimensional_facts
                        ],
                    }
                ]
                section.warnings.extend(
                    f"{item.metric_id}/{item.dimension_name}: 勾稽或来源待核验"
                    for item in dimensional_facts
                    if item.verification_status == VerificationStatus.PENDING
                )
            elif number == 2:
                section.facts = [_format_fact(fact) for fact in facts]
                section.method_refs.extend(method_refs[item] for item in FINANCIAL_METHOD_IDS)
                section.tables = [
                    {
                        "name": "最新原始指标",
                        "rows": [
                            {"metric_id": key, "value": value, "lineage": "对应事实表中的fact_id"}
                            for key, value in latest.items()
                        ],
                    },
                    {
                        "name": "代码派生指标",
                        "rows": [
                            {
                                "metric_id": key,
                                "value": value,
                                "claim_kind": "代码推导",
                                "method_ref": _derived_method_ref(key, method_refs),
                            }
                            for key, value in derived.items()
                        ],
                    },
                    {"name": "报表勾稽检查", "rows": data_quality_checks},
                ]
                section.warnings.extend(
                    item["message"] for item in data_quality_checks if item.get("status") != "OK"
                )
            elif number == 5:
                section.tables = [
                    {"name": "估值结果", "rows": [item.model_dump(mode="json") for item in valuations]},
                    {"name": "模型运行与输入血缘", "rows": [item.model_dump(mode="json") for item in model_runs]},
                ]
                section.method_refs.extend(item.method_ref for item in model_runs)
                section.warnings.extend(item.failure_reason for item in model_runs if item.failure_reason)
            elif number == 7:
                section.warnings.extend(route_warnings)
            elif number == 8:
                section.tables = [{"name": "三情景测算", "rows": scenarios}]
                unconfirmed = [item["scenario"] for item in scenarios if item.get("status") == "已计算" and not item.get("confirmed")]
                if unconfirmed:
                    section.warnings.append(f"以下情景尚未人工确认: {', '.join(unconfirmed)}")
            section_events = [
                item
                for item in events
                if number
                in self.event_taxonomy["event_types"]
                .get(item.event_type, {})
                .get("report_steps", [])
            ]
            if section_events:
                current_ids = {
                    item.event_id for item in current_event_versions(section_events)
                }
                section.tables.append(
                    {
                        "name": "公司事件与状态链",
                        "rows": [
                            _format_event(
                                item,
                                self.event_taxonomy["event_types"].get(
                                    item.event_type, {}
                                ),
                                item.event_id in current_ids,
                            )
                            for item in section_events
                        ],
                    }
                )
                event_types = sorted(
                    {
                        str(
                            self.event_taxonomy["event_types"]
                            .get(item.event_type, {})
                            .get("label")
                            or item.event_type
                        )
                        for item in section_events
                    }
                )
                if not section_claims:
                    prefix = section.summary.rstrip("。")
                    section.summary = (
                        f"{prefix}；已载入{len(section_events)}条可追溯公司事件，"
                        f"覆盖{', '.join(event_types)}。事件仅作为披露事实展示，"
                        "评价仍需单独形成证据化结论。"
                    )
                section.warnings.extend(
                    f"{item.event_type}/{item.event_id}: 事件来源待核验"
                    for item in section_events
                    if item.verification_status == VerificationStatus.PENDING
                )
            result.append(section)
        return result

    def _conclusion(
        self,
        request: ReportCreateRequest,
        claims: list[ClaimRecord],
        valuations: list,
        scenarios: list[dict[str, Any]],
        conflicts: list[str],
        completeness: float,
        confidence: float,
    ) -> ConclusionCard:
        successful = [
            item
            for item in valuations
            if item.status == ModelRunStatus.SUCCESS and item.fair_value_per_share is not None
        ]
        scenario_values = {item.get("scenario"): item.get("fair_value_per_share") for item in scenarios if item.get("status") == "已计算"}
        scenario_numeric = [value for value in scenario_values.values() if isinstance(value, (int, float)) and value > 0]
        low = min(scenario_numeric) if scenario_numeric else None
        base = scenario_values.get("基准")
        high = max(scenario_numeric) if scenario_numeric else None
        warnings_spread = False
        if successful:
            primary = successful[0]
            if base is None:
                base = primary.fair_value_per_share
            if low is None:
                low = primary.range_low
            if high is None:
                high = primary.range_high
            all_values = [item.fair_value_per_share for item in successful if item.fair_value_per_share is not None]
            if (
                len(all_values) > 1
                and min(all_values) > 0
                and max(all_values) / min(all_values) > self.policy.method_spread_block_ratio
            ):
                warnings_spread = True
                low = min([*all_values, *([low] if low else [])])
                high = max([*all_values, *([high] if high else [])])
        margin = None
        if request.current_price is not None and request.current_price > 0 and base and base > 0:
            margin = 1 - request.current_price / base

        rating = request.requested_rating
        if (
            rating == ResearchRating.UNRATED
            and base
            and request.current_price is not None
            and request.current_price > 0
            and not conflicts
            and confidence >= self.policy.minimum_confidence
            and not warnings_spread
        ):
            upside = base / request.current_price - 1
            rating = (
                ResearchRating.POSITIVE
                if upside >= self.policy.positive_upside
                else ResearchRating.CAUTIOUS
                if upside <= self.policy.cautious_downside
                else ResearchRating.NEUTRAL
            )
        if conflicts or warnings_spread or confidence < min(0.35, self.policy.minimum_confidence):
            rating = ResearchRating.UNRATED

        def texts(categories: set[str], limit: int = 3) -> list[str]:
            return [claim.text for claim in claims if claim.category.lower() in categories][:limit]

        invalidations = texts({"invalidation"}, 5)
        if request.input_metadata.get("report_bridge_version"):
            invalidations = sorted({text for claim in claims for text in claim.invalidation_conditions})
        tracking = sorted({item.tracking_metric for item in request.assumptions if item.tracking_metric})
        return ConclusionCard(
            rating=rating,
            rating_confirmed=False,
            current_price=request.current_price,
            price_as_of=request.price_as_of,
            fair_value_low=low,
            fair_value_base=base,
            fair_value_high=high,
            margin_of_safety=margin,
            core_theses=texts({"business", "moat", "driver", "strategy"}),
            major_risks=texts({"risk"}),
            catalysts=texts({"catalyst"}),
            evidence_completeness=completeness,
            evidence_confidence=confidence,
            invalidation_conditions=invalidations,
            next_tracking_items=tracking,
        )

    def _data_quality_checks(
        self,
        facts: list[FactRecord],
        latest: dict[str, float],
        method_refs: dict[str, str],
        *,
        research_coverage: dict[str, Any] | None = None,
        materialization_selected_ids: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        periods = (research_coverage or {}).get("periods") or (
            (research_coverage or {}).get("analysis_scope") or {}
        )
        annual_target = (
            len(periods.get("annual", ()))
            if isinstance(periods, dict) and periods.get("annual")
            else 5
        )
        quarter_target = (
            len(periods.get("quarters", ()))
            if isinstance(periods, dict) and periods.get("quarters")
            else 12
        )
        annual_revenue_periods = {
            item.period_end
            for item in facts
            if item.metric_id == "revenue" and item.period_type == "annual" and item.value is not None
        }
        quarter_revenue_periods = {
            item.period_end
            for item in facts
            if item.metric_id == "revenue" and item.period_type == "single_quarter" and item.value is not None
        }
        checks.extend(
            [
                {
                    "check": f"{annual_target}年年度覆盖",
                    "status": "OK" if len(annual_revenue_periods) >= annual_target else "WARN",
                    "actual_difference": len(annual_revenue_periods) - annual_target,
                    "tolerance": 0,
                    "message": f"营业收入年度期数={len(annual_revenue_periods)}，目标不少于{annual_target}",
                    "method_ref": method_refs["FIN.NORMALIZATION"],
                },
                {
                    "check": f"{quarter_target}个单季覆盖",
                    "status": "OK" if len(quarter_revenue_periods) >= quarter_target else "WARN",
                    "actual_difference": len(quarter_revenue_periods) - quarter_target,
                    "tolerance": 0,
                    "message": f"营业收入单季度期数={len(quarter_revenue_periods)}，目标不少于{quarter_target}",
                    "method_ref": method_refs["FIN.NORMALIZATION"],
                },
            ]
        )
        critical = [
            item
            for item in self._critical_facts_in_analysis_window(
                facts, annual_target=annual_target
            )
            if item.value is not None
        ]
        dual_count = sum(
            item.verification_status == VerificationStatus.DUAL_SOURCE for item in critical
        )
        supplier_count = sum(
            item.verification_status == VerificationStatus.SUPPLIER_DIRECT
            and is_fact_consumable(
                item,
                materialization_selected_ids=materialization_selected_ids,
            )
            for item in critical
        )
        consumable_count = sum(
            is_fact_consumable(
                item,
                materialization_selected_ids=materialization_selected_ids,
            )
            for item in critical
        )
        checks.append(
            {
                "check": "关键字段统一事实准入",
                "status": "OK" if critical and consumable_count == len(critical) else "WARN",
                "actual_difference": consumable_count - len(critical),
                "tolerance": 0,
                "message": (
                    f"关键字段可消费={consumable_count}/{len(critical)}；"
                    f"双源一致={dual_count}；供应商直采={supplier_count}"
                ),
                "method_ref": "source_policy.json",
            }
        )
        if {"total_assets", "total_liabilities", "total_equity"} <= latest.keys():
            result = reconcile_balance_sheet(
                latest["total_assets"], latest["total_liabilities"], latest["total_equity"]
            )
            checks.append(
                {
                    "check": "资产负债恒等式",
                    "status": "OK" if result.passed else "FAIL",
                    "actual_difference": result.difference,
                    "tolerance": result.tolerance,
                    "message": result.message,
                    "method_ref": method_refs["FIN.BALANCE"],
                }
            )
        cash_keys = (
            "opening_cash",
            "operating_cash_flow",
            "investing_cash_flow",
            "financing_cash_flow",
            "fx_effect",
            "closing_cash",
        )
        if set(cash_keys) <= latest.keys():
            result = reconcile_cash_flow(*(latest[key] for key in cash_keys))
            checks.append(
                {
                    "check": "现金流衔接",
                    "status": "OK" if result.passed else "FAIL",
                    "actual_difference": result.difference,
                    "tolerance": result.tolerance,
                    "message": result.message,
                    "method_ref": method_refs["FIN.CASHFLOW"],
                }
            )
        return checks

    def _critical_facts_in_analysis_window(
        self, facts: list[FactRecord], *, annual_target: int = 5
    ) -> list[FactRecord]:
        """Limit acceptance checks to the report's configured annual window.

        A selected annual filing also contains the preceding year's comparison
        column.  That raw value is useful lineage, but it sits outside the
        requested annual periods and must not inflate the dual-source
        denominator.
        """

        annual_periods = sorted(
            {
                item.period_end
                for item in facts
                if item.metric_id == "revenue"
                and item.period_type == "annual"
                and item.period_end is not None
            }
        )
        window_start = (
            annual_periods[-annual_target]
            if len(annual_periods) >= annual_target
            else None
        )
        return [
            item
            for item in facts
            if item.metric_id in self.source_policy.critical_metrics
            and (window_start is None or item.period_end is None or item.period_end >= window_start)
        ]


def _latest_numeric_facts(
    facts: list[FactRecord],
    *,
    materialization_selected_ids: frozenset[str] | None = None,
) -> dict[str, float]:
    latest = latest_consumable_facts(
        facts,
        materialization_selected_ids=materialization_selected_ids,
    )
    return {metric_id: fact.value for metric_id, fact in latest.items() if fact.value is not None}


def _format_fact(fact: FactRecord) -> str:
    value = "暂无该数据" if fact.value is None else f"{fact.value:g}{fact.unit}"
    period = fact.period_end.isoformat() if fact.period_end else fact.as_of.date().isoformat()
    return f"{fact.metric_id}: {value}（{period}，{fact.verification_status.value}，fact:{fact.fact_id}）"


def _format_dimensional_fact(fact: DimensionalFactRecord) -> dict[str, Any]:
    evidence = fact.evidence_spans[0] if fact.evidence_spans else None
    return {
        "dimensional_fact_id": fact.dimensional_fact_id,
        "period_end": fact.period_end.isoformat() if fact.period_end else None,
        "dimension_type": fact.dimension_type,
        "dimension_category": DIMENSION_TYPE_NAMES.get(
            fact.dimension_type,
            fact.dimension_type,
        ),
        "dimension_name": fact.dimension_name,
        "metric_id": fact.metric_id,
        "value": fact.value,
        "unit": fact.unit,
        "verification_status": fact.verification_status.value,
        "source_ids": fact.source_ids,
        "document_id": evidence.document_id if evidence else None,
        "page": evidence.page if evidence else fact.document_page,
        "table_title": evidence.table_title if evidence else fact.table_title,
        "row_label": evidence.row_label if evidence else fact.row_label,
        "column_label": evidence.column_label if evidence else fact.column_label,
        "lineage": f"dimensional_fact:{fact.dimensional_fact_id}",
    }


def _format_event(
    event: EventRecord,
    taxonomy_spec: dict[str, Any],
    is_current: bool,
) -> dict[str, Any]:
    pages = list(
        dict.fromkeys(
            str(item.page)
            for item in event.evidence_spans
            if item.page is not None
        )
    )
    evidence = "\n---\n".join(
        item.text.strip()
        for item in event.evidence_spans
        if item.text and item.text.strip()
    )
    return {
        "event_id": event.event_id,
        "事件类别": taxonomy_spec.get("label") or event.event_type,
        "event_type": event.event_type,
        "event_subtype": event.event_subtype,
        "状态": event.lifecycle_state,
        "当前状态": "是" if is_current else "否",
        "公告时间": event.announced_at.isoformat(),
        "生效时间": event.effective_at.isoformat() if event.effective_at else None,
        "摘要": event.summary,
        "金额": event.amount,
        "币种": event.currency,
        "股数": event.shares,
        "比例": event.ratio,
        "重要性": event.materiality,
        "核验状态": event.verification_status.value,
        "root_event_id": event.root_event_id,
        "previous_event_id": event.previous_event_id,
        "source_ids": event.source_ids,
        "document_ids": event.document_ids,
        "页码": pages,
        "证据片段": evidence or None,
        "事件条款": event.event_terms,
        "影响指标": event.affected_metrics,
        "lineage": f"event:{event.event_id}",
    }


def _section_summary(number: int, claims: list[ClaimRecord]) -> str:
    if claims:
        return "；".join(item.text for item in claims[:3])
    defaults = {
        1: "尚未录入经证据支持的业务与商业模式结论。",
        2: "财务数字由结构化事实和确定性公式生成。",
        3: "尚未录入经证据支持的治理结论。",
        4: "尚未录入经证据支持的资本行为结论。",
        5: "估值仅在输入完整且模型适用时生成。",
        6: "尚未录入核心驱动、催化剂、风险及失效条件。",
        7: "行业结论需绑定市场规模、供需或竞争证据。",
        8: "情景结果取决于经人工确认的经营与估值假设。",
    }
    return defaults[number]


def _snapshot_id(
    request: ReportCreateRequest,
    facts: list[FactRecord],
    sources: list,
    claims: list[ClaimRecord],
    dimensional_facts: list[DimensionalFactRecord],
    events: list[EventRecord],
) -> str:
    payload = {
        "ticker": request.ticker,
        "as_of": request.as_of.isoformat(),
        "facts": [fact.model_dump(mode="json") for fact in facts],
        "sources": [source.model_dump(mode="json") for source in sources],
        "claims": [claim.model_dump(mode="json") for claim in claims],
        "dimensional_facts": [
            item.model_dump(mode="json") for item in dimensional_facts
        ],
        "events": [item.model_dump(mode="json") for item in events],
        "industry_facts": [item.model_dump(mode="json") for item in request.industry_facts if _datetime_le(item.available_at, request.as_of)],
        "peer_sets": [item.model_dump(mode="json") for item in request.peer_sets if _datetime_le(item.as_of, request.as_of)],
        "research_coverage": request.research_coverage,
        "input_metadata": request.input_metadata,
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"ds-{digest[:20]}"


def _datetime_le(left: datetime, right: datetime) -> bool:
    def aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    return aware(left) <= aware(right)


def _normalize_lineage(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, refs in value.items():
        if isinstance(refs, str):
            refs = [refs]
        if isinstance(refs, list):
            result[str(key)] = sorted({str(item) for item in refs if item})
    return result


def _contains_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, (list, tuple)):
        return any(_contains_number(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_number(item) for item in value.values())
    return False


def _numeric_input_keys(inputs: dict[str, Any]) -> set[str]:
    return {key for key, value in inputs.items() if _contains_number(value)}


def _derived_method_ref(metric_id: str, method_refs: dict[str, str]) -> str:
    mapping = {
        "gross_margin": "FIN.INCOME",
        "net_margin": "FIN.INCOME",
        "parent_net_margin": "FIN.INCOME",
        "adjusted_net_margin": "FIN.INCOME",
        "selling_expense_ratio": "FIN.INCOME",
        "administrative_expense_ratio": "FIN.INCOME",
        "rd_ratio": "FIN.INCOME",
        "finance_expense_ratio": "FIN.INCOME",
        "free_cash_flow": "FIN.CASHFLOW",
        "cash_profit_ratio": "FIN.CASHFLOW",
        "operating_cash_flow_margin": "FIN.CASHFLOW",
        "capex_to_revenue": "FIN.CASHFLOW",
        "net_working_capital": "FIN.FCF_WORKING_CAPITAL",
        "operating_working_capital": "FIN.FCF_WORKING_CAPITAL",
        "accrual_ratio": "FIN.EARNINGS_QUALITY",
        "adjusted_profit_share": "FIN.EARNINGS_QUALITY",
        "impairment_to_revenue": "FIN.EARNINGS_QUALITY",
        "roic": "FIN.ROIC_WACC",
    }
    if metric_id.startswith("dupont_"):
        return method_refs["FIN.DUPONT"]
    if metric_id in {"current_ratio", "net_debt", "interest_bearing_debt_ratio", "goodwill_to_equity", "receivable_to_revenue", "inventory_to_cost"}:
        return method_refs["FIN.BALANCE"]
    return method_refs[mapping.get(metric_id, "STEP.FINANCIAL")]


def _tracking_indicators(request: ReportCreateRequest) -> list[TrackingIndicator]:
    grouped: dict[str, list] = {}
    for item in request.assumptions:
        if item.tracking_metric:
            grouped.setdefault(item.tracking_metric, []).append(item)
    result = []
    for name, assumptions in sorted(grouped.items()):
        base = next((item for item in assumptions if item.scenario.value == "基准"), assumptions[0])
        result.append(
            TrackingIndicator(
                name=name,
                current_value=base.value,
                unit=base.unit,
                threshold="实际值偏离基准假设时触发",
                source_ids=sorted({source_id for item in assumptions for source_id in item.source_ids}),
            )
        )
    return result


def _validate_request_lineage(request: ReportCreateRequest) -> list[str]:
    errors: list[str] = []
    source_ids = [item.source_id for item in request.sources]
    fact_ids = [item.fact_id for item in request.facts]
    dimensional_fact_ids = [
        item.dimensional_fact_id for item in request.dimensional_facts
    ]
    event_ids = [item.event_id for item in request.events]
    assumption_ids = [item.assumption_id for item in request.assumptions]
    for label, values in (
        ("source_id", source_ids),
        ("fact_id", fact_ids),
        ("dimensional_fact_id", dimensional_fact_ids),
        ("event_id", event_ids),
        ("assumption_id", assumption_ids),
    ):
        duplicates = sorted({item for item in values if values.count(item) > 1})
        if duplicates:
            errors.append(f"重复{label}: {', '.join(duplicates)}")
    source_map = {item.source_id: item for item in request.sources}
    fact_id_set = set(fact_ids)
    for fact in request.facts:
        missing = sorted(set(fact.source_ids) - set(source_map))
        if missing:
            errors.append(f"{fact.fact_id}: 来源不存在 {', '.join(missing)}")
        missing_parents = sorted(set(fact.derived_from_fact_ids) - fact_id_set)
        if missing_parents:
            errors.append(f"{fact.fact_id}: 派生事实不存在 {', '.join(missing_parents)}")
        if fact.verification_status == VerificationStatus.DUAL_SOURCE:
            sources = [source_map[item] for item in fact.source_ids if item in source_map]
            independent = any(are_independent(left, right) for index, left in enumerate(sources) for right in sources[index + 1 :])
            if len(sources) < 2 or not independent:
                errors.append(f"{fact.fact_id}: 双源一致状态缺少两个独立上游来源")
    for fact in request.dimensional_facts:
        missing = sorted(set(fact.source_ids) - set(source_map))
        if missing:
            errors.append(
                f"{fact.dimensional_fact_id}: 来源不存在 {', '.join(missing)}"
            )
        if fact.verification_status == VerificationStatus.DUAL_SOURCE:
            sources = [
                source_map[item] for item in fact.source_ids if item in source_map
            ]
            independent = any(
                are_independent(left, right)
                for index, left in enumerate(sources)
                for right in sources[index + 1 :]
            )
            if len(sources) < 2 or not independent:
                errors.append(
                    f"{fact.dimensional_fact_id}: 双源一致状态缺少两个独立上游来源"
                )
    for event in request.events:
        missing = sorted(set(event.source_ids) - set(source_map))
        if missing:
            errors.append(f"{event.event_id}: 事件来源不存在 {', '.join(missing)}")
        missing_facts = sorted(set(event.supporting_fact_ids) - fact_id_set)
        if missing_facts:
            errors.append(
                f"{event.event_id}: 事件支持事实不存在 {', '.join(missing_facts)}"
            )
        if event.verification_status == VerificationStatus.DUAL_SOURCE:
            sources = [
                source_map[item] for item in event.source_ids if item in source_map
            ]
            independent = any(
                are_independent(left, right)
                for index, left in enumerate(sources)
                for right in sources[index + 1 :]
            )
            if len(sources) < 2 or not independent:
                errors.append(
                    f"{event.event_id}: 双源一致状态缺少两个独立上游来源"
                )
    for item in [*request.industry_facts, *request.peer_sets]:
        missing_sources = set(item.source_ids) - set(source_map)
        if missing_sources:
            errors.append(f"行业或同行输入来源不存在: {sorted(missing_sources)}")
    for assumption in request.assumptions:
        if not assumption.source_ids:
            errors.append(f"{assumption.assumption_id}: 情景假设必须关联来源或用户假设记录")
        missing = sorted(set(assumption.source_ids) - set(source_map))
        if missing:
            errors.append(f"{assumption.assumption_id}: 假设来源不存在 {', '.join(missing)}")
        if assumption.valid_until is None:
            errors.append(f"{assumption.assumption_id}: 情景假设必须注明有效期")
    return errors


def _validate_point_in_time_lineage(
    request: ReportCreateRequest,
    facts: list[FactRecord],
    claims: list[ClaimRecord],
    sources: list,
    dimensional_facts: list[DimensionalFactRecord],
    events: list[EventRecord],
) -> list[str]:
    errors = validate_claims(claims, facts, sources)
    source_ids = {item.source_id for item in sources}
    fact_ids = {item.fact_id for item in facts}
    for fact in facts:
        missing_sources = sorted(set(fact.source_ids) - source_ids)
        if missing_sources:
            errors.append(
                f"{fact.fact_id}: 截至报告时点来源尚不可用 {', '.join(missing_sources)}"
            )
        missing_parents = sorted(set(fact.derived_from_fact_ids) - fact_ids)
        if missing_parents:
            errors.append(
                f"{fact.fact_id}: 截至报告时点派生事实尚不可用 {', '.join(missing_parents)}"
            )
    for fact in dimensional_facts:
        missing_sources = sorted(set(fact.source_ids) - source_ids)
        if missing_sources:
            errors.append(
                f"{fact.dimensional_fact_id}: 截至报告时点来源尚不可用 "
                f"{', '.join(missing_sources)}"
            )
    for event in events:
        missing_sources = sorted(set(event.source_ids) - source_ids)
        if missing_sources:
            errors.append(
                f"{event.event_id}: 截至报告时点事件来源尚不可用 "
                f"{', '.join(missing_sources)}"
            )
        missing_facts = sorted(set(event.supporting_fact_ids) - fact_ids)
        if missing_facts:
            errors.append(
                f"{event.event_id}: 截至报告时点事件支持事实尚不可用 "
                f"{', '.join(missing_facts)}"
            )
    for assumption in request.assumptions:
        missing_sources = sorted(set(assumption.source_ids) - source_ids)
        if missing_sources:
            errors.append(
                f"{assumption.assumption_id}: 截至报告时点假设来源尚不可用 {', '.join(missing_sources)}"
            )
    return errors
