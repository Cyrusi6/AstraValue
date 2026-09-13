from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


RULE_VERSION = "reading-rules-v1"


def select_research_document(entry, *, as_of, question_ids=(), trigger_reason=None):
    """目录元数据决策；不为判型预下载正文。缺主体/日期时保持待核实。"""
    import re
    from analysis.filing_parser import detect_filing_period
    from analysis.acquisition.content_selection import content_exclusion_reason, NO_AUDIT_ENGLISH_ANNUAL_V1
    from .scope import SCOPE_ID
    title = entry.get("title", "")
    url = entry.get("resource_url", entry.get("url", ""))
    mime = entry.get("expected_mime_types", ("application/pdf",))
    period = detect_filing_period(re.sub(r"英文(?:版)?|english", "", title, flags=re.I))
    excluded = content_exclusion_reason(title, url, mime, NO_AUDIT_ENGLISH_ANNUAL_V1)
    decision = {"scope_id": SCOPE_ID, "document_class": "D20", "selected": False,
                "question_ids": list(question_ids), "reason": "catalog_only", "period": None,
                "language": "en" if re.search(r"英文|english", title, re.I) else "zh"}
    if excluded:
        return decision | {"document_class": "D21", "reason": excluded}
    if "摘要" in title:
        return decision | {"document_class": "D04", "reason": "summary_cannot_close_full_report_gap"}
    if period:
        end = period.period_end
        decision["period"] = end.isoformat()
        category = "D01" if end.month == 12 else "D02" if end.month == 6 else "D03"
        baseline = (category == "D01" and as_of.year-5 <= end.year < as_of.year) or (
            category == "D02" and end.year in (as_of.year, as_of.year-1) and end <= as_of)
        published = str(entry.get("published_at") or "")[:10]
        known_subject = bool((entry.get("metadata") or {}).get("ticker") or entry.get("ticker"))
        if not known_subject or not published:
            return decision | {"document_class": category, "reason": "subject_or_publication_missing"}
        if published > as_of.isoformat():
            return decision | {"document_class": category, "reason": "published_after_cutoff"}
        correction = bool(re.search(r"更正|修订|补充", title))
        selected = (baseline and not correction and decision["language"] == "zh") or bool(question_ids and trigger_reason)
        return decision | {"document_class": category, "selected": selected,
            "reason": "required_full_report" if selected and baseline else trigger_reason or "outside_required_periods"}
    categories = {
        "D05": r"招股|上市公告书|章程", "D06": r"业绩预告|业绩快报",
        "D07": r"经营数据|产销|产能|订单|调价", "D08": r"利润分配|权益分派|分红",
        "D09": r"回购", "D10": r"权益变动|收购报告书|增持|减持|质押|冻结|解禁",
        "D11": r"募集资金|募投|增发|配股", "D12": r"债券|可转债|付息|赎回|转股",
        "D13": r"并购|重组|资产购买|资产出售|业绩承诺", "D14": r"激励|员工持股|行权|归属",
        "D15": r"关联交易|资金占用|担保", "D16": r"问询|关注函|处罚|诉讼|仲裁|整改|风险",
        "D17": r"任免|聘任|审计机构|内部控制|治理", "D18": r"投资者关系|说明会|活动记录",
        "D19": r"统计公报|统计表|行业报告|利率曲线|政策",
    }
    for category, pattern in categories.items():
        if re.search(pattern, title):
            decision["document_class"] = category
            break
    if question_ids and trigger_reason and decision["document_class"] != "D20":
        decision.update(selected=True, reason=trigger_reason)
    return decision


@dataclass(frozen=True)
class CatalogDecision:
    material_id: str
    title: str
    catalog_status: str
    body_status: str
    reason: str


def classify_catalog_entry(material_id: str, title: str) -> CatalogDecision:
    normalized = "".join(title.split())
    lowered = normalized.lower()
    if "摘要" in normalized:
        return CatalogDecision(material_id, title, "cataloged", "not_selected", "report_summary")
    from analysis.acquisition.content_selection import is_english_annual_report, is_standalone_audit_pdf
    if is_english_annual_report(title, "https://example.invalid/report.pdf", ("application/pdf",)):
        return CatalogDecision(material_id, title, "cataloged", "not_selected", "english_report")
    if is_standalone_audit_pdf(title, "https://example.invalid/report.pdf", ("application/pdf",)):
        return CatalogDecision(
            material_id,
            title,
            "cataloged",
            "not_selected",
            "standalone_audit_report",
        )
    if "年度报告" in normalized or "半年度报告" in normalized:
        return CatalogDecision(
            material_id,
            title,
            "cataloged",
            "selected_mandatory",
            "complete_chinese_annual_or_interim_report",
        )
    return CatalogDecision(material_id, title, "cataloged", "not_selected", "no_body_trigger")


@dataclass(frozen=True)
class RuleEvaluation:
    rule_id: str
    rule_version: str
    triggered: bool
    reason: str
    target_sections: tuple[str, ...]
    input_values: Mapping[str, Any] = field(default_factory=dict)
    missing_inputs: tuple[str, ...] = ()


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _ratio_change(current: Any, previous: Any) -> Decimal | None:
    current_value = _decimal(current)
    previous_value = _decimal(previous)
    if current_value is None or previous_value is None or previous_value <= 0:
        return None
    return current_value / previous_value - Decimal("1")


class ReadingRuleEngine:
    """R01-R12 pure scheduler rules; no rating or analysis side effects."""

    def __init__(self, *, version: str = RULE_VERSION) -> None:
        self.version = version

    def evaluate(self, values: Mapping[str, Any]) -> tuple[RuleEvaluation, ...]:
        return tuple(
            evaluator(values)
            for evaluator in (
                self._r01,
                self._r02,
                self._r03,
                self._r04,
                self._r05,
                self._r06,
                self._r07,
                self._r08,
                self._r09,
                self._r10,
                self._r11,
                self._r12,
            )
        )

    def _result(
        self,
        rule_id: str,
        triggered: bool,
        reason: str,
        sections: tuple[str, ...],
        values: Mapping[str, Any],
        missing: Iterable[str] = (),
    ) -> RuleEvaluation:
        return RuleEvaluation(
            rule_id,
            self.version,
            triggered,
            reason,
            sections,
            dict(values),
            tuple(sorted(set(missing))),
        )

    def _r01(self, v: Mapping[str, Any]) -> RuleEvaluation:
        triggered: list[str] = []
        missing: list[str] = []
        observed: dict[str, Any] = {}
        for metric in ("revenue", "parent_net_profit", "net_income_excl"):
            current = _decimal(v.get(f"{metric}.current"))
            prior = _decimal(v.get(f"{metric}.prior_year"))
            previous_yoy = _decimal(v.get(f"{metric}.previous_quarter_yoy"))
            if current is None or prior is None:
                missing.append(f"{metric}.same_period")
                continue
            if metric != "revenue" and ((prior <= 0 < current) or (prior >= 0 > current)):
                triggered.append(f"{metric}:profit_loss_switch")
                continue
            yoy = _ratio_change(current, prior)
            if yoy is None:
                missing.append(f"{metric}.positive_yoy_base")
                continue
            observed[f"{metric}.yoy"] = str(yoy)
            if abs(yoy) >= Decimal("0.20"):
                triggered.append(f"{metric}:abs_yoy>=20%")
            if previous_yoy is not None and abs(yoy - previous_yoy) >= Decimal("0.15"):
                triggered.append(f"{metric}:yoy_change>=15pp")
        return self._result(
            "R01",
            bool(triggered),
            ";".join(triggered) or "threshold_not_met",
            ("经营情况讨论", "收入与利润变化"),
            observed,
            missing,
        )

    def _r02(self, v: Mapping[str, Any]) -> RuleEvaluation:
        thresholds = {
            "gross_margin": Decimal("0.03"),
            "sales_expense_ratio": Decimal("0.02"),
            "management_expense_ratio": Decimal("0.02"),
            "research_expense_ratio": Decimal("0.02"),
        }
        triggered: list[str] = []
        missing: list[str] = []
        observed: dict[str, Any] = {}
        for metric, threshold in thresholds.items():
            current = _decimal(v.get(f"{metric}.current"))
            prior = _decimal(v.get(f"{metric}.prior_year"))
            if current is None or prior is None:
                missing.append(f"{metric}.same_period")
                continue
            change = current - prior
            observed[f"{metric}.change_pp"] = str(change)
            if abs(change) >= threshold:
                triggered.append(f"{metric}:change>={threshold}")
        return self._result(
            "R02",
            bool(triggered),
            ";".join(triggered) or "threshold_not_met",
            ("产品与渠道结构", "成本费用与研发"),
            observed,
            missing,
        )

    def _r03(self, v: Mapping[str, Any]) -> RuleEvaluation:
        profit = _decimal(v.get("parent_net_profit.current"))
        cfo = _decimal(v.get("operating_cash_flow.current"))
        prior_ratio = _decimal(v.get("cfo_to_net_profit.prior_year"))
        missing = [
            name
            for name, value in (
                ("parent_net_profit.current", profit),
                ("operating_cash_flow.current", cfo),
            )
            if value is None
        ]
        reasons: list[str] = []
        observed: dict[str, Any] = {}
        if profit is not None and cfo is not None:
            if profit > 0 and cfo < 0:
                reasons.append("positive_profit_but_negative_cfo")
            if profit > 0:
                ratio = cfo / profit
                observed["cfo_to_net_profit.current"] = str(ratio)
                if ratio < Decimal("0.8"):
                    if prior_ratio is None:
                        missing.append("cfo_to_net_profit.prior_year")
                    elif prior_ratio - ratio >= Decimal("0.2"):
                        reasons.append("cfo_to_profit_below_0.8_and_down_0.2")
            else:
                missing.append("positive_net_profit_denominator")
        return self._result(
            "R03",
            bool(reasons),
            ";".join(reasons) or "threshold_not_met",
            ("现金流说明", "应收与合同负债"),
            observed,
            missing,
        )

    def _r04(self, v: Mapping[str, Any]) -> RuleEvaluation:
        reasons: list[str] = []
        missing: list[str] = []
        for metric in ("accounts_receivable", "inventory"):
            asset_growth = _decimal(v.get(f"{metric}.yoy"))
            revenue_growth = _decimal(v.get("revenue.yoy"))
            days = _decimal(v.get(f"{metric}.turnover_days"))
            prior_days = _decimal(v.get(f"{metric}.prior_turnover_days"))
            if asset_growth is not None and revenue_growth is not None:
                if asset_growth - revenue_growth >= Decimal("0.20"):
                    reasons.append(f"{metric}:growth_excess>=20pp")
            else:
                missing.append(f"{metric}.growth_comparison")
            if days is not None and prior_days is not None and prior_days > 0:
                if days - prior_days >= Decimal("30") and days / prior_days - 1 >= Decimal("0.20"):
                    reasons.append(f"{metric}:turnover_days_deteriorated")
            else:
                missing.append(f"{metric}.turnover_days_comparison")
        return self._result("R04", bool(reasons), ";".join(reasons) or "threshold_not_met", ("应收", "存货", "渠道产销"), {}, missing)

    def _r05(self, v: Mapping[str, Any]) -> RuleEvaluation:
        reasons: list[str] = []
        missing: list[str] = []
        decline = _ratio_change(v.get("contract_liability.current"), v.get("contract_liability.prior_year"))
        if decline is None:
            missing.append("contract_liability.same_period_positive_base")
        elif decline <= Decimal("-0.20"):
            reasons.append("contract_liability_down>=20%")
        debt = _decimal(v.get("interest_bearing_debt.current"))
        assets = _decimal(v.get("total_assets.current"))
        prior_ratio = _decimal(v.get("interest_bearing_debt_ratio.prior_year"))
        if debt is None or assets is None or assets <= 0 or prior_ratio is None or not v.get("interest_bearing_debt.complete_scope"):
            missing.append("complete_interest_bearing_debt_ratio")
        elif debt / assets - prior_ratio >= Decimal("0.05"):
            reasons.append("interest_bearing_debt_ratio_up>=5pp")
        return self._result("R05", bool(reasons), ";".join(reasons) or "threshold_not_met", ("合同负债", "债务与现金流附注"), {}, missing)

    def _r06(self, v: Mapping[str, Any]) -> RuleEvaluation:
        reasons: list[str] = []
        missing: list[str] = []
        goodwill_impairment = _decimal(v.get("goodwill_impairment.current"))
        if goodwill_impairment is not None and goodwill_impairment > 0:
            reasons.append("positive_goodwill_impairment")
        impairment = _decimal(v.get("impairment_loss.current"))
        equity = _decimal(v.get("parent_equity.prior_period"))
        if impairment is None or equity is None or equity <= 0:
            missing.append("impairment_loss_and_positive_parent_equity")
        elif impairment / equity >= Decimal("0.01"):
            reasons.append("impairment>=1%_parent_equity")
        return self._result("R06", bool(reasons), ";".join(reasons) or "threshold_not_met", ("资产组与减值",), {}, missing)

    def _event_rule(self, rule_id: str, v: Mapping[str, Any], keys: tuple[str, ...], sections: tuple[str, ...]) -> RuleEvaluation:
        active = [key for key in keys if bool(v.get(key))]
        return self._result(rule_id, bool(active), ";".join(active) or "no_matching_event", sections, {key: v.get(key) for key in keys})

    def _r07(self, v: Mapping[str, Any]) -> RuleEvaluation:
        return self._event_rule("R07", v, ("controller_changed", "chairman_changed", "general_manager_changed", "cfo_changed", "auditor_changed", "audit_opinion_changed"), ("公司治理", "审计与控制权"))

    def _r08(self, v: Mapping[str, Any]) -> RuleEvaluation:
        amount = _decimal(v.get("capital_event.amount"))
        equity = _decimal(v.get("parent_equity.prior_period"))
        reasons = []
        missing = []
        if v.get("capital_event.major") or v.get("capital_event.linked_question"):
            reasons.append("major_or_question_linked")
        if amount is None or equity is None or equity <= 0:
            missing.append("capital_event_amount_and_positive_parent_equity")
        elif amount / equity >= Decimal("0.05"):
            reasons.append("capital_event>=5%_parent_equity")
        return self._result("R08", bool(reasons), ";".join(reasons) or "threshold_not_met", ("交易资金用途", "承诺与状态"), {}, missing)

    def _r09(self, v: Mapping[str, Any]) -> RuleEvaluation:
        amount = _decimal(v.get("risk_event.amount"))
        equity = _decimal(v.get("parent_equity.prior_period"))
        reasons = []
        missing = []
        if v.get("risk_event.key_regulatory"):
            reasons.append("key_regulatory_or_control_event")
        if amount is None or equity is None or equity <= 0:
            missing.append("risk_event_amount_and_positive_parent_equity")
        elif amount / equity >= Decimal("0.01"):
            reasons.append("risk_event>=1%_parent_equity")
        return self._result("R09", bool(reasons), ";".join(reasons) or "threshold_not_met", ("担保诉讼监管公告",), {}, missing)

    def _r10(self, v: Mapping[str, Any]) -> RuleEvaluation:
        reasons = []
        missing = []
        if v.get("dividend.current_completed") and v.get("dividend.prior_completed"):
            decline = _ratio_change(v.get("dividend.current_per_share"), v.get("dividend.prior_per_share"))
            if decline is None:
                missing.append("completed_dividend_per_share_positive_base")
            elif decline <= Decimal("-0.20"):
                reasons.append("completed_dividend_down>=20%")
        else:
            missing.append("two_completed_profit_year_dividends")
        if v.get("dividend.cancelled") or v.get("repurchase.cancelled"):
            reasons.append("capital_return_plan_cancelled")
        if v.get("repurchase.purpose_changed"):
            reasons.append("repurchase_purpose_changed")
        upper = _decimal(v.get("repurchase.planned_shares_upper"))
        shares = _decimal(v.get("total_shares.same_point"))
        if upper is not None and shares is not None and shares > 0 and upper / shares >= Decimal("0.01"):
            reasons.append("repurchase_plan_upper>=1%_shares")
        return self._result("R10", bool(reasons), ";".join(reasons) or "threshold_not_met", ("分红回购与资本回报",), {}, missing)

    def _r11(self, v: Mapping[str, Any]) -> RuleEvaluation:
        linked = bool(v.get("correction.linked_used_material"))
        substantive = any(bool(v.get(key)) for key in ("correction.substantive_revision", "correction.anomaly", "correction.open_question"))
        return self._result("R11", linked and substantive, "targeted_related_correction" if linked and substantive else "unrelated_or_immaterial_correction", ("受影响章节",), {})

    def _r12(self, v: Mapping[str, Any]) -> RuleEvaluation:
        keys = ("event.affects_question", "event.affects_assumption", "event.affects_risk_tracking", "event.affects_invalidation")
        return self._event_rule("R12", v, keys, ("具体研究问题证据章节",))


@dataclass(frozen=True)
class ReadingCandidate:
    company_id: str
    material_id: str
    content_hash: str
    report_period: str | None
    evaluation: RuleEvaluation
    input_fact_ids: tuple[str, ...] = ()
    unanswered_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadingTask:
    task_key: str
    company_id: str
    material_id: str
    content_hash: str
    report_period: str | None
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    target_sections: tuple[str, ...]
    input_fact_ids: tuple[str, ...]
    unanswered_questions: tuple[str, ...]
    body_artifact_id: str | None
    derived_artifact_id: str | None
    download_required: bool
    mineru_required: bool
    state: str


@dataclass(frozen=True)
class ReadingEvidence:
    evidence_id: str
    company_id: str
    question_id: str
    requirement_id: str
    route_id: str
    report_period: str
    document_id: str
    content_hash: str
    field_name: str
    value: Any
    unit: str | None
    page: int | None = None
    html_locator: str | None = None
    quality: str = "passed"
    explicit_absence: bool = False
    coverage_start: date | None = None
    coverage_end: date | None = None

    @property
    def has_locator(self) -> bool:
        return self.page is not None or bool(self.html_locator)


@dataclass(frozen=True)
class ReadingRequirementResolution:
    requirement_id: str
    report_period: str
    readiness: str
    evidence_ids: tuple[str, ...]
    value: Any = None
    unit: str | None = None
    reason_code: str | None = None
    negative_fact: bool = False


def resolve_reading_requirement(
    *,
    company_id: str,
    question_id: str,
    requirement_id: str,
    route_id: str,
    report_period: str,
    evidence: Iterable[ReadingEvidence] = (),
    parsed_content_available: bool = False,
) -> ReadingRequirementResolution:
    """Admit only an exact, located answer; a parse or keyword hit is not an answer."""

    matches = tuple(
        item
        for item in evidence
        if item.company_id == company_id
        and item.question_id == question_id
        and item.requirement_id == requirement_id
        and item.route_id == route_id
        and item.report_period == report_period
    )
    valid = tuple(
        item
        for item in matches
        if item.quality == "passed"
        and item.document_id
        and item.content_hash
        and item.field_name
        and item.has_locator
        and (item.value is not None or item.explicit_absence)
    )
    if not valid:
        reason = (
            "parsed_but_requirement_unanswered"
            if parsed_content_available
            else "reading_material_or_answer_missing"
        )
        return ReadingRequirementResolution(
            requirement_id,
            report_period,
            "reading_pending",
            (),
            reason_code=reason,
        )
    values = {(item.value, item.unit, item.explicit_absence) for item in valid}
    if len(values) != 1:
        return ReadingRequirementResolution(
            requirement_id,
            report_period,
            "reading_pending",
            tuple(sorted(item.evidence_id for item in valid)),
            reason_code="conflicting_located_answers",
        )
    chosen = valid[0]
    if chosen.explicit_absence and (
        chosen.coverage_start is None or chosen.coverage_end is None
    ):
        return ReadingRequirementResolution(
            requirement_id,
            report_period,
            "reading_pending",
            tuple(sorted(item.evidence_id for item in valid)),
            reason_code="negative_fact_period_unbounded",
        )
    return ReadingRequirementResolution(
        requirement_id,
        report_period,
        "ready",
        tuple(sorted(item.evidence_id for item in valid)),
        False if chosen.explicit_absence else chosen.value,
        chosen.unit,
        "explicit_absence_for_period" if chosen.explicit_absence else None,
        chosen.explicit_absence,
    )


def merge_reading_candidates(
    candidates: Iterable[ReadingCandidate],
    *,
    existing_bodies: Mapping[str, str] | None = None,
    existing_parses: Mapping[str, str] | None = None,
) -> tuple[ReadingTask, ...]:
    body_by_hash = dict(existing_bodies or {})
    parse_by_hash = dict(existing_parses or {})
    grouped: dict[tuple[str, str, str], list[ReadingCandidate]] = {}
    for candidate in candidates:
        if not candidate.evaluation.triggered:
            continue
        grouped.setdefault(
            (candidate.company_id, candidate.material_id, candidate.content_hash), []
        ).append(candidate)

    tasks: list[ReadingTask] = []
    for key, group in sorted(grouped.items()):
        company_id, material_id, content_hash = key
        body_id = body_by_hash.get(content_hash)
        parse_id = parse_by_hash.get(content_hash)
        tasks.append(
            ReadingTask(
                task_key=f"{company_id}|{material_id}|{content_hash}",
                company_id=company_id,
                material_id=material_id,
                content_hash=content_hash,
                report_period=next((item.report_period for item in group if item.report_period), None),
                rule_ids=tuple(sorted({item.evaluation.rule_id for item in group})),
                reasons=tuple(sorted({item.evaluation.reason for item in group})),
                target_sections=tuple(sorted({section for item in group for section in item.evaluation.target_sections})),
                input_fact_ids=tuple(sorted({fact for item in group for fact in item.input_fact_ids})),
                unanswered_questions=tuple(sorted({question for item in group for question in item.unanswered_questions})),
                body_artifact_id=body_id,
                derived_artifact_id=parse_id,
                download_required=body_id is None and parse_id is None,
                mineru_required=parse_id is None,
                state="queued",
            )
        )
    return tuple(tasks)


__all__ = [
    "CatalogDecision",
    "ReadingCandidate",
    "ReadingEvidence",
    "ReadingRequirementResolution",
    "ReadingRuleEngine",
    "ReadingTask",
    "RuleEvaluation",
    "classify_catalog_entry",
    "merge_reading_candidates",
    "resolve_reading_requirement",
]
