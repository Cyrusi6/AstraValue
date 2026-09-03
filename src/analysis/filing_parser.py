from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .models import DocumentRecord, FactRecord, VerificationStatus


FLOW_METRICS = {
    "revenue",
    "cost_of_revenue",
    "net_income",
    "net_income_parent",
    "net_income_excl",
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "capital_expenditure",
    "selling_expense",
    "administrative_expense",
    "rd_expense",
    "finance_expense",
    "minority_interest",
    "asset_impairment_loss",
    "fx_effect",
}


@dataclass(frozen=True)
class FilingPeriod:
    year: int
    kind: str
    period_end: date

    @property
    def quarter(self) -> int:
        return {"q1": 1, "h1": 2, "q3": 3, "annual": 4}[self.kind]


def detect_filing_period(title: str) -> FilingPeriod | None:
    compact = re.sub(r"\s+", "", title)
    if any(token in compact for token in ("摘要", "英文版", "取消", "社会责任", "审计报告")):
        return None
    match = re.search(r"(20\d{2})年", compact)
    if not match:
        return None
    year = int(match.group(1))
    if "第一季度报告" in compact or "一季度报告" in compact:
        return FilingPeriod(year, "q1", date(year, 3, 31))
    if "半年度报告" in compact or "中期报告" in compact:
        return FilingPeriod(year, "h1", date(year, 6, 30))
    if "第三季度报告" in compact or "三季度报告" in compact:
        return FilingPeriod(year, "q3", date(year, 9, 30))
    if "年度报告" in compact or re.search(r"\d{4}年年报(?:（.*?）)?$", compact):
        return FilingPeriod(year, "annual", date(year, 12, 31))
    return None


def parse_official_document(document: DocumentRecord) -> list[FactRecord]:
    period = detect_filing_period(document.title)
    if period is None:
        return []
    text = Path(document.text_path).read_text(encoding="utf-8", errors="replace")
    lines = _clean_lines(text.splitlines(), period)
    facts = _parse_summary_table(lines, document, period)
    existing = {(item.metric_id, item.period_type, item.period_end) for item in facts}
    facts.extend(_parse_financial_statements(lines, document, period, existing))
    return facts


def derive_single_quarter_facts(facts: list[FactRecord], limit: int = 12) -> list[FactRecord]:
    """Turn Q1/H1/Q3/YTD flows into auditable single-quarter facts."""

    by_key = {
        (item.metric_id, item.period_end, item.period_type): item
        for item in facts
        if item.metric_id in FLOW_METRICS and item.period_end and item.value is not None
    }
    derived: list[FactRecord] = []
    years = sorted({item.period_end.year for item in facts if item.period_end})
    metrics = sorted({item.metric_id for item in facts if item.metric_id in FLOW_METRICS})
    for metric_id in metrics:
        for year in years:
            q1_end = date(year, 3, 31)
            h1_end = date(year, 6, 30)
            q3_end = date(year, 9, 30)
            annual_end = date(year, 12, 31)
            q1 = by_key.get((metric_id, q1_end, "cumulative"))
            h1 = by_key.get((metric_id, h1_end, "cumulative"))
            q3 = by_key.get((metric_id, q3_end, "cumulative"))
            annual = by_key.get((metric_id, annual_end, "annual"))
            direct_q1 = by_key.get((metric_id, q1_end, "single_quarter"))
            direct_q3 = by_key.get((metric_id, q3_end, "single_quarter"))
            if direct_q1 is None and q1 is not None:
                derived.append(_derived_quarter(q1, None, 1))
            if h1 is not None and q1 is not None:
                derived.append(_derived_quarter(h1, q1, 2))
            if direct_q3 is None and q3 is not None and h1 is not None:
                derived.append(_derived_quarter(q3, h1, 3))
            if annual is not None and q3 is not None:
                derived.append(_derived_quarter(annual, q3, 4))

    combined = [*facts, *derived]
    keep_single_ids: set[str] = set()
    grouped: dict[str, list[FactRecord]] = defaultdict(list)
    for item in combined:
        if item.period_type == "single_quarter" and item.period_end:
            grouped[item.metric_id].append(item)
    for items in grouped.values():
        selected = sorted(items, key=lambda item: (item.period_end, item.disclosed_at or item.as_of), reverse=True)[:limit]
        keep_single_ids.update(item.fact_id for item in selected)
    return [
        item
        for item in combined
        if item.period_type != "single_quarter" or item.fact_id in keep_single_ids
    ]


def _derived_quarter(current: FactRecord, previous: FactRecord | None, quarter: int) -> FactRecord:
    value = current.value if previous is None else current.value - previous.value
    period_end = date(current.period_end.year, quarter * 3, 31 if quarter in {1, 4} else 30)
    period_start = date(current.period_end.year, (quarter - 1) * 3 + 1, 1)
    parents = [current, *([previous] if previous else [])]
    source_ids = sorted({source_id for item in parents for source_id in item.source_ids})
    statuses = {item.verification_status for item in parents}
    if VerificationStatus.PENDING in statuses:
        status = VerificationStatus.PENDING
    elif statuses == {VerificationStatus.DUAL_SOURCE}:
        status = VerificationStatus.DUAL_SOURCE
    elif VerificationStatus.ESTIMATED in statuses:
        status = VerificationStatus.ESTIMATED
    else:
        status = VerificationStatus.AUTHORITATIVE_SINGLE
    return FactRecord(
        ticker=current.ticker,
        metric_id=current.metric_id,
        value=value,
        unit=current.unit,
        currency=current.currency,
        period_start=period_start,
        period_end=period_end,
        period_type="single_quarter",
        disclosed_at=max((item.disclosed_at for item in parents if item.disclosed_at), default=None),
        as_of=max(item.as_of for item in parents),
        scope=current.scope,
        audited=all(item.audited is True for item in parents),
        original_label="累计值确定性转单季",
        source_ids=source_ids,
        verification_status=status,
        method_ref="FIN.NORMALIZATION@1.0.0",
        derived_from_fact_ids=[item.fact_id for item in parents],
        metadata={
            "quarter": quarter,
            "calculation": "current_ytd - previous_ytd" if previous else "Q1 YTD",
        },
    )


def _parse_summary_table(
    lines: list[str], document: DocumentRecord, period: FilingPeriod
) -> list[FactRecord]:
    compact = _summary_section(lines)
    if not compact:
        return []
    scale, source_unit = _unit_scale(compact[:500])
    aliases = {
        "revenue": ("营业收入", "营业总收入"),
        "net_income_parent": ("归属于上市公司股东的净利润",),
        "net_income_excl": ("归属于上市公司股东的扣除非经常性损益的净利润",),
        "operating_cash_flow": ("经营活动产生的现金流量净额", "经营活动产生的现金流"),
        "eps_basic": ("基本每股收益（元/股）", "基本每股收益(元/股)", "基本每股收益"),
        "roe": ("加权平均净资产收益率（%）", "加权平均净资产收益率(%)", "加权平均净资产收益率"),
        "total_assets": ("总资产",),
        "total_parent_equity": ("归属于上市公司股东的所有者权益", "归属于上市公司股东的净资产"),
    }
    positions: dict[str, tuple[int, int, str]] = {}
    for metric_id, candidates in aliases.items():
        matches = []
        for alias in candidates:
            pattern = r"\s*".join(re.escape(char) for char in alias)
            match = re.search(pattern, compact)
            if match:
                matches.append((match.start(), match.end(), alias))
        if matches:
            positions[metric_id] = min(matches)
    ordered_positions = sorted(
        (position, matched_end, metric_id, alias)
        for metric_id, (position, matched_end, alias) in positions.items()
    )
    facts: list[FactRecord] = []
    for index, (position, matched_end, metric_id, alias) in enumerate(ordered_positions):
        end = ordered_positions[index + 1][0] if index + 1 < len(ordered_positions) else min(len(compact), position + 700)
        segment = compact[matched_end:end]
        values = _numbers(segment)
        if not values:
            continue
        if metric_id in {"total_assets", "total_parent_equity"}:
            facts.append(_fact(document, period, metric_id, values[0] * scale, "元", "instant", source_unit, alias))
            continue
        if metric_id == "eps_basic":
            value = values[2] if period.kind == "q3" and len(values) >= 3 else values[0]
            facts.append(_fact(document, period, metric_id, value, "元/股", "reported", "元/股", alias))
            continue
        if metric_id == "roe":
            value = values[2] if period.kind == "q3" and len(values) >= 3 else values[0]
            facts.append(_fact(document, period, metric_id, value / 100, "ratio", "reported", "%", alias))
            continue
        if period.kind == "annual":
            facts.append(_fact(document, period, metric_id, values[0] * scale, "元", "annual", source_unit, alias))
        elif period.kind == "q3":
            if metric_id != "operating_cash_flow" and len(values) >= 3:
                facts.append(_fact(document, period, metric_id, values[0] * scale, "元", "single_quarter", source_unit, alias))
                cumulative_value = values[2]
            else:
                cumulative_value = values[0]
            facts.append(_fact(document, period, metric_id, cumulative_value * scale, "元", "cumulative", source_unit, alias))
        else:
            facts.append(_fact(document, period, metric_id, values[0] * scale, "元", "cumulative", source_unit, alias))
            if period.kind == "q1":
                facts.append(_fact(document, period, metric_id, values[0] * scale, "元", "single_quarter", source_unit, alias))
    return facts


def _parse_financial_statements(
    lines: list[str],
    document: DocumentRecord,
    period: FilingPeriod,
    existing: set[tuple[str, str, date | None]],
) -> list[FactRecord]:
    facts: list[FactRecord] = []
    balance = _statement_lines(lines, "合并资产负债表", ("合并利润表", "母公司资产负债表"))
    if balance:
        scale, source_unit = _unit_scale("".join(balance[:30]))
        balance_rows = {
            "cash": ("货币资金",),
            "accounts_receivable": ("应收账款",),
            "inventory": ("存货",),
            "goodwill": ("商誉",),
            "current_assets": ("流动资产合计",),
            "current_liabilities": ("流动负债合计",),
            "total_assets": ("资产总计", "资产合计"),
            "total_liabilities": ("负债合计",),
            "total_equity": ("所有者权益（或股东权益）合计", "所有者权益合计", "股东权益合计"),
            "accounts_payable": ("应付账款",),
            "contract_assets": ("合同资产",),
            "shares_outstanding": ("实收资本（或股本）", "股本"),
        }
        for metric_id, aliases in balance_rows.items():
            values, label = _row_values(balance, aliases)
            unit = "股" if metric_id == "shares_outstanding" else "元"
            if values and (metric_id, "instant", period.period_end) not in existing:
                facts.append(_fact(document, period, metric_id, values[0] * scale, unit, "instant", source_unit, label))
            if period.kind == "annual" and len(values) >= 2:
                facts.append(
                    _comparative_fact(
                        document, period, metric_id, values[1] * scale, unit, "instant", source_unit, label
                    )
                )
        debt_values = []
        debt_labels = []
        for aliases in (
            ("短期借款",),
            ("一年内到期的非流动负债",),
            ("长期借款",),
            ("应付债券",),
            ("租赁负债",),
        ):
            value, label = _row_value(balance, aliases)
            if value is not None:
                debt_values.append(value)
                debt_labels.append(label)
        if debt_values:
            facts.append(
                _fact(
                    document,
                    period,
                    "interest_bearing_debt",
                    sum(debt_values) * scale,
                    "元",
                    "instant",
                    source_unit,
                    "+".join(debt_labels),
                    metadata={"components": debt_labels},
                )
            )

    statement_type = "annual" if period.kind == "annual" else "cumulative"
    income = _statement_lines(lines, "合并利润表", ("合并现金流量表", "母公司利润表"))
    if income:
        scale, source_unit = _unit_scale("".join(income[:30]))
        income_rows = {
            "revenue": ("营业收入", "营业总收入"),
            "cost_of_revenue": ("营业成本",),
            "net_income": ("净利润",),
            "net_income_parent": ("归属于母公司股东的净利润", "归属于母公司所有者的净利润"),
            "selling_expense": ("销售费用",),
            "administrative_expense": ("管理费用",),
            "rd_expense": ("研发费用",),
            "finance_expense": ("财务费用",),
            "minority_interest": ("少数股东损益",),
            "asset_impairment_loss": ("资产减值损失",),
        }
        for metric_id, aliases in income_rows.items():
            values, label = _row_values(income, aliases)
            if values and (metric_id, statement_type, period.period_end) not in existing:
                facts.append(_fact(document, period, metric_id, values[0] * scale, "元", statement_type, source_unit, label))
            if period.kind == "annual" and len(values) >= 2:
                facts.append(
                    _comparative_fact(
                        document, period, metric_id, values[1] * scale, "元", "annual", source_unit, label
                    )
                )

    cashflow = _statement_lines(lines, "合并现金流量表", ("母公司现金流量表", "合并所有者权益变动表"))
    if cashflow:
        scale, source_unit = _unit_scale("".join(cashflow[:30]))
        cash_rows = {
            "operating_cash_flow": ("经营活动产生的现金流量净额",),
            "investing_cash_flow": ("投资活动产生的现金流量净额",),
            "financing_cash_flow": ("筹资活动产生的现金流量净额",),
            "capital_expenditure": ("购建固定资产、无形资产和其他长期资产支付的现金",),
            "fx_effect": ("汇率变动对现金及现金等价物的影响",),
            "opening_cash": ("期初现金及现金等价物余额", "现金及现金等价物期初余额"),
            "closing_cash": ("期末现金及现金等价物余额", "现金及现金等价物期末余额"),
        }
        for metric_id, aliases in cash_rows.items():
            values, label = _row_values(cashflow, aliases)
            output_type = "instant" if metric_id in {"opening_cash", "closing_cash"} else statement_type
            if values and (metric_id, output_type, period.period_end) not in existing:
                facts.append(_fact(document, period, metric_id, values[0] * scale, "元", output_type, source_unit, label))
            if period.kind == "annual" and output_type == "annual" and len(values) >= 2:
                facts.append(
                    _comparative_fact(
                        document, period, metric_id, values[1] * scale, "元", "annual", source_unit, label
                    )
                )
    return facts


def _fact(
    document: DocumentRecord,
    period: FilingPeriod,
    metric_id: str,
    value: float,
    unit: str,
    period_type: str,
    source_unit: str,
    original_label: str,
    metadata: dict | None = None,
) -> FactRecord:
    quarter = period.quarter
    if period_type == "single_quarter":
        period_start = date(period.year, (quarter - 1) * 3 + 1, 1)
    elif period_type in {"annual", "cumulative"}:
        period_start = date(period.year, 1, 1)
    else:
        period_start = None
    return FactRecord(
        ticker=document.ticker,
        metric_id=metric_id,
        value=value,
        unit=unit,
        period_start=period_start,
        period_end=period.period_end,
        period_type=period_type,
        disclosed_at=document.source.published_at,
        # `as_of` is the point at which this fact became publicly available;
        # extraction time is retained by DocumentRecord.extracted_at.
        as_of=document.source.published_at or document.extracted_at,
        audited=period.kind == "annual",
        original_label=original_label,
        document_page=_page_for_label(document, original_label),
        source_ids=[document.source.source_id],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        metadata={
            "filing_kind": period.kind,
            "source_unit": source_unit,
            "parser": "official_pdf_v1",
            **(metadata or {}),
        },
    )


def _comparative_fact(
    document: DocumentRecord,
    reporting_period: FilingPeriod,
    metric_id: str,
    value: float,
    unit: str,
    period_type: str,
    source_unit: str,
    original_label: str,
) -> FactRecord:
    prior_period = FilingPeriod(
        reporting_period.year - 1,
        "annual",
        date(reporting_period.year - 1, 12, 31),
    )
    fact = _fact(
        document,
        prior_period,
        metric_id,
        value,
        unit,
        period_type,
        source_unit,
        original_label,
        metadata={
            "comparative_from_filing_year": reporting_period.year,
            "comparative_value": True,
        },
    )
    return fact.model_copy(
        update={
            "is_restated": True,
            "restatement_version": f"{reporting_period.year}-annual-comparative",
            "original_label": f"{original_label}（上年比较数）",
        }
    )


def _summary_section(lines: list[str]) -> str:
    preserved = [re.sub(r"\s+", " ", item.strip()).replace("：", ":") for item in lines]
    normalized = [_compact(item) for item in preserved]
    starts = [index for index, item in enumerate(normalized) if "主要会计数据和财务指标" in item]
    if not starts:
        starts = [index for index, item in enumerate(normalized) if "主要财务数据" in item]
    if not starts:
        return ""
    start = starts[-1] if len(starts) > 1 and starts[0] < 30 else starts[0]
    end = min(len(lines), start + 300)
    for index in range(start + 1, end):
        if any(token in normalized[index] for token in ("非经常性损益项目", "主要会计数据、财务指标发生变动", "主要会计数据和财务指标发生变动")):
            end = index
            break
    # Keep a delimiter between adjacent numeric lines. Concatenating PDF lines
    # would silently turn `300,000.00` + `280,000.00` into `300,000.00280,000.00`.
    return "\n".join(preserved[start:end])


def _statement_lines(lines: list[str], heading: str, end_headings: tuple[str, ...]) -> list[str]:
    normalized = [_compact(item) for item in lines]
    candidates = [index for index, item in enumerate(normalized) if heading in item]
    start = None
    for candidate in candidates:
        nearby = "".join(normalized[candidate : candidate + 35])
        if "单位" in nearby and ("编制单位" in nearby or "项目" in nearby):
            start = candidate
            break
    if start is None:
        return []
    end = min(len(lines), start + 900)
    for index in range(start + 10, end):
        if any(token in normalized[index] for token in end_headings):
            end = index
            break
    return lines[start:end]


def _row_values(lines: list[str], aliases: tuple[str, ...]) -> tuple[list[float], str]:
    normalized = [_compact(item) for item in lines]
    for alias in aliases:
        target = _compact(alias)
        for index in range(len(normalized)):
            matched_end = None
            for width in range(1, 5):
                if index + width > len(normalized):
                    break
                joined = "".join(normalized[index : index + width])
                cleaned = re.sub(r"^(?:[一二三四五六七八九十]+、|\(?[一二三四五六七八九十0-9]+\)?[.、])", "", joined)
                cleaned = re.sub(r"^其中[:：]?", "", cleaned)
                # Cash-flow statements commonly prefix reconciliation rows with
                # “加：” or “减：”.  The prefix is presentation, not part of the
                # accounting label, and must not prevent exact row matching.
                cleaned = re.sub(r"^(?:加|减):?", "", cleaned)
                if cleaned == target or cleaned.startswith(target + "（") or cleaned.startswith(target + "("):
                    matched_end = index + width
                    break
            if matched_end is None:
                continue
            candidates: list[tuple[str, float]] = []
            for cursor in range(matched_end, min(len(normalized), matched_end + 8)):
                token = normalized[cursor]
                raw_token = lines[cursor].strip()
                if not token or token in {"项目", "不适用", "-", "--"}:
                    continue
                if re.fullmatch(r"\d{1,3}[（(]\d+[）)]", token):
                    # A statement-note reference such as ``56（2）`` is not a
                    # monetary value.  Continue to the actual value columns.
                    continue
                # Older PDF text layers often put the current and comparative
                # columns on one physical line.  Preserve each numeric cell
                # instead of requiring the entire line to be one number.
                numeric_cells = _number_items(raw_token)
                if numeric_cells:
                    candidates.extend(numeric_cells)
                    continue
                if re.search(r"[\u4e00-\u9fff]", token):
                    if not candidates and (
                        token.startswith(("（", "("))
                        or token.endswith(("）", ")"))
                        or "填列" in token
                    ):
                        continue
                    break
            if candidates:
                if (
                    len(candidates) >= 2
                    and re.fullmatch(r"\d{1,3}", candidates[0][0])
                    and abs(candidates[1][1]) >= 1_000
                ):
                    candidates = candidates[1:]
                return [value for _, value in candidates], alias
    return [], aliases[0]


def _row_value(lines: list[str], aliases: tuple[str, ...]) -> tuple[float | None, str]:
    values, label = _row_values(lines, aliases)
    return (values[0] if values else None), label


def _unit_scale(text: str) -> tuple[float, str]:
    compact = _compact(text)
    for label, factor in (("亿元", 100_000_000.0), ("百万元", 1_000_000.0), ("万元", 10_000.0), ("千元", 1_000.0)):
        if f"单位：{label}" in compact or f"单位:{label}" in compact:
            return factor, label
    return 1.0, "元"


def _numbers(text: str) -> list[float]:
    return [value for _, value in _number_items(text)]


def _number_items(text: str) -> list[tuple[str, float]]:
    results: list[tuple[str, float]] = []
    for match in re.finditer(r"(?<![A-Za-z\d])(?:\()?[-+]?\d[\d,]*(?:\.\d+)?(?:\))?", text):
        value = _single_number(match.group())
        if value is not None and math.isfinite(value):
            results.append((match.group(), value))
    return results


def _single_number(text: str) -> float | None:
    compact = text.strip().replace("，", ",").replace(" ", "")
    if not re.fullmatch(r"\(?[-+]?\d[\d,]*(?:\.\d+)?\)?", compact):
        return None
    negative_parentheses = compact.startswith("(") and compact.endswith(")")
    compact = compact.strip("()").replace(",", "")
    try:
        value = float(compact)
    except ValueError:
        return None
    return -abs(value) if negative_parentheses else value


def _clean_lines(lines: list[str], period: FilingPeriod) -> list[str]:
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("--- page"):
            continue
        if re.fullmatch(r"\d+\s*/\s*\d+", stripped):
            continue
        compact = _compact(stripped)
        if str(period.year) in compact and "报告" in compact and len(compact) < 80:
            continue
        result.append(stripped)
    return result


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":")


def _page_for_label(document: DocumentRecord, label: str) -> int | None:
    try:
        text = Path(document.text_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    target = _compact(label)
    page_markers = list(re.finditer(r"--- page (\d+) ---", text, flags=re.I))
    for index, marker in enumerate(page_markers):
        start = marker.end()
        end = page_markers[index + 1].start() if index + 1 < len(page_markers) else len(text)
        if target and target in _compact(text[start:end]):
            return int(marker.group(1))
    # The exact page is optional; the archived document and label still preserve lineage.
    return None
