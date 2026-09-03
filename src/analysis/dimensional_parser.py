from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .filing_parser import detect_filing_period
from .models import (
    DimensionalFactRecord,
    DisclosureForm,
    DocumentRecord,
    EvidenceSpan,
    ExtractionMethod,
    VerificationStatus,
)


PARSER_VERSION = "1.1.0"
NUMBER_PATTERN = r"[-+−]?\d[\d,，]*(?:\.\d+)?"
ROW_DISCLOSED_UNIT = "__row_disclosed_unit__"


@dataclass(frozen=True)
class IndexedLine:
    text: str
    start: int
    end: int
    page: int | None

    @property
    def compact(self) -> str:
        return re.sub(r"\s+", "", self.text).strip()


@dataclass(frozen=True)
class ParsedRow:
    name: str
    values: tuple[float, ...]
    lines: tuple[IndexedLine, ...]
    unit: str | None = None


@dataclass(frozen=True)
class OutputColumn:
    index: int
    metric_id: str
    column_label: str
    unit: str
    period_type: str
    transform: Callable[[float], float]
    currency: str = ""


COMMON_HEADER_FRAGMENTS = (
    "分行业",
    "分产品",
    "分地区",
    "销售模式",
    "营业收入",
    "营业成本",
    "毛利率",
    "比上年",
    "上年增减",
    "年增减",
    "增减",
    "百分点",
    "个百分点",
    "分点",
    "增减（%）",
    "增减(%)",
    "减（%）",
    "减(%)",
    "主要产品",
    "主要工厂名称",
    "设计产能",
    "实际产能",
    "在建产能",
    "产能利用率",
    "产能",
    "生产量",
    "产量",
    "销售量",
    "库存量",
)


def parse_dimensional_facts(
    document: DocumentRecord,
    *,
    data_snapshot_id: str,
) -> list[DimensionalFactRecord]:
    """Parse directly disclosed operating dimensions from one annual report."""

    period = detect_filing_period(document.title)
    if period is None or period.kind != "annual":
        return []
    text = Path(document.text_path).read_text(encoding="utf-8", errors="replace")
    lines = _indexed_lines(text)
    period_start = date(period.year, 1, 1)
    period_end = period.period_end
    facts: list[DimensionalFactRecord] = []

    main_heading_index = _find_line(
        lines,
        "主营业务分行业、分产品、分地区、分销售模式情况",
    )
    money_scale, source_unit = _money_scale_near(lines, main_heading_index)
    segment_columns = (
        OutputColumn(
            0,
            "segment_revenue",
            "营业收入",
            "元",
            "annual",
            lambda value, scale=money_scale: value * scale,
            "CNY",
        ),
        OutputColumn(
            1,
            "segment_cost",
            "营业成本",
            "元",
            "annual",
            lambda value, scale=money_scale: value * scale,
            "CNY",
        ),
        OutputColumn(
            2,
            "segment_margin",
            "毛利率",
            "ratio",
            "annual",
            lambda value: value / 100.0,
        ),
    )
    for heading, end_heading, dimension_type in (
        ("主营业务分行业情况", "主营业务分产品情况", "business"),
        ("主营业务分产品情况", "主营业务分地区情况", "product"),
        ("主营业务分地区情况", "主营业务分销售模式情况", "region"),
        ("主营业务分销售模式情况", "产销量情况分析表", "channel"),
    ):
        rows, table_line = _fixed_width_rows(
            lines,
            heading=heading,
            end_headings=(end_heading,),
            expected_values=6,
        )
        if table_line is None:
            continue
        for row in rows:
            facts.extend(
                _row_facts(
                    document=document,
                    snapshot_id=data_snapshot_id,
                    period_start=period_start,
                    period_end=period_end,
                    dimension_type=dimension_type,
                    parent_dimension="主营业务",
                    table_title=heading,
                    row=row,
                    columns=segment_columns,
                    accounting_basis="company_disclosed_main_business",
                    metadata={
                        "source_unit": source_unit,
                        "reconciliation_scope": "complete",
                    },
                )
            )

    if not any(item.metric_id == "segment_revenue" for item in facts):
        facts.extend(
            _szse_segment_facts(
                lines,
                document=document,
                snapshot_id=data_snapshot_id,
                period_start=period_start,
                period_end=period_end,
            )
        )

    production_rows, _ = _fixed_width_rows(
        lines,
        heading="产销量情况分析表",
        end_headings=("重大采购合同", "成本分析表"),
        expected_values=6,
    )
    production_columns = (
        OutputColumn(
            0,
            "production_volume",
            "生产量",
            ROW_DISCLOSED_UNIT,
            "annual",
            float,
        ),
        OutputColumn(
            1,
            "sales_volume",
            "销售量",
            ROW_DISCLOSED_UNIT,
            "annual",
            float,
        ),
        OutputColumn(
            2,
            "ending_inventory_volume",
            "库存量",
            ROW_DISCLOSED_UNIT,
            "instant",
            float,
        ),
    )
    for row in production_rows:
        facts.extend(
            _row_facts(
                document=document,
                snapshot_id=data_snapshot_id,
                period_start=period_start,
                period_end=period_end,
                dimension_type="product",
                parent_dimension="产销量情况",
                table_title="产销量情况分析表",
                row=row,
                columns=production_columns,
                accounting_basis="company_disclosed_operating_volume",
                metadata={},
            )
        )

    if not production_rows:
        facts.extend(
            _physical_volume_facts(
                lines,
                document=document,
                snapshot_id=data_snapshot_id,
                period_start=period_start,
                period_end=period_end,
            )
        )

    capacity_rows, capacity_heading = _fixed_width_rows(
        lines,
        heading="现有产能",
        end_headings=("说明", "在建产能", "产能计算标准"),
        expected_values=2,
    )
    capacity_unit = _disclosed_unit_near(lines, capacity_heading)
    capacity_columns = (
        OutputColumn(
            0,
            "design_capacity",
            "设计产能",
            capacity_unit or ROW_DISCLOSED_UNIT,
            "instant",
            float,
        ),
        OutputColumn(
            1,
            "actual_capacity",
            "实际产能",
            capacity_unit or ROW_DISCLOSED_UNIT,
            "annual",
            float,
        ),
    )
    for row in capacity_rows:
        facts.extend(
            _row_facts(
                document=document,
                snapshot_id=data_snapshot_id,
                period_start=period_start,
                period_end=period_end,
                dimension_type="capacity",
                parent_dimension="现有产能",
                table_title="现有产能",
                row=row,
                columns=capacity_columns,
                accounting_basis="company_disclosed_capacity",
                metadata={"source_unit": capacity_unit},
            )
        )

    if not capacity_rows:
        facts.extend(
            _capacity_summary_facts(
                lines,
                document=document,
                snapshot_id=data_snapshot_id,
                period_start=period_start,
                period_end=period_end,
            )
        )

    facts.extend(
        _concentration_facts(
            text,
            document=document,
            snapshot_id=data_snapshot_id,
            period_start=period_start,
            period_end=period_end,
        )
    )
    return _validate_dimensional_facts(facts)


def _indexed_lines(text: str) -> list[IndexedLine]:
    result: list[IndexedLine] = []
    page: int | None = None
    offset = 0
    for raw in text.splitlines(keepends=True):
        value = raw.rstrip("\r\n")
        marker = re.fullmatch(r"\s*--- page (?P<page>\d+) ---\s*", value)
        if marker:
            page = int(marker.group("page"))
        result.append(
            IndexedLine(
                text=value.strip(),
                start=offset,
                end=offset + len(value),
                page=page,
            )
        )
        offset += len(raw)
    return result


def _find_line(
    lines: list[IndexedLine],
    needle: str,
    *,
    start: int = 0,
) -> int | None:
    target = _compact(needle)
    for index in range(start, len(lines)):
        if target in lines[index].compact:
            return index
    return None


def _fixed_width_rows(
    lines: list[IndexedLine],
    *,
    heading: str,
    end_headings: tuple[str, ...],
    expected_values: int,
) -> tuple[list[ParsedRow], IndexedLine | None]:
    start_index = _find_line(lines, heading)
    if start_index is None:
        return [], None
    end_index = len(lines)
    for end_heading in end_headings:
        candidate = _find_line(lines, end_heading, start=start_index + 1)
        if candidate is not None:
            end_index = min(end_index, candidate)
    rows: list[ParsedRow] = []
    label_lines: list[IndexedLine] = []
    value_lines: list[IndexedLine] = []
    values: list[float] = []
    row_unit: str | None = None
    for line in lines[start_index + 1 : end_index]:
        numeric = _data_values(line.text)
        if numeric is not None:
            if not label_lines:
                continue
            values.extend(numeric)
            value_lines.append(line)
            if len(values) >= expected_values:
                name = _normalize_dimension_name("".join(item.compact for item in label_lines))
                if name:
                    rows.append(
                        ParsedRow(
                            name=name,
                            values=tuple(values[:expected_values]),
                            lines=tuple([*label_lines, *value_lines]),
                            unit=row_unit,
                        )
                    )
                label_lines = []
                value_lines = []
                values = []
                row_unit = None
            continue
        if label_lines and _looks_like_disclosed_unit(line.text):
            row_unit = _normalize_disclosed_unit(line.text)
            value_lines.append(line)
            continue
        if _skip_table_line(line):
            if _resets_row_label(line):
                label_lines = []
                value_lines = []
                values = []
                row_unit = None
            continue
        if _looks_like_row_label(line.text):
            if values:
                label_lines = []
                value_lines = []
                values = []
                row_unit = None
            label_lines.append(line)
    return rows, lines[start_index]


def _szse_segment_facts(
    lines: list[IndexedLine],
    *,
    document: DocumentRecord,
    snapshot_id: str,
    period_start: date,
    period_end: date,
) -> list[DimensionalFactRecord]:
    heading = "占公司营业收入或营业利润10%以上的行业、产品、地区、销售模式的情况"
    start_index = _find_line(lines, heading)
    if start_index is None:
        return []
    end_index = len(lines)
    for end_heading in (
        "公司主营业务数据统计口径",
        "公司实物销售收入是否大于劳务收入",
    ):
        candidate = _find_line(lines, end_heading, start=start_index + 1)
        if candidate is not None:
            end_index = min(end_index, candidate)

    group_headers = {
        "分业务": "business",
        "分行业": "business",
        "分客户所处行业": "business",
        "分产品": "product",
        "分地区": "region",
        "分销售模式": "channel",
    }
    current_type: str | None = None
    label_lines: list[IndexedLine] = []
    value_lines: list[IndexedLine] = []
    values: list[float] = []
    grouped_rows: list[tuple[str, ParsedRow]] = []
    for line in lines[start_index + 1 : end_index]:
        compact = line.compact
        group_type = group_headers.get(compact)
        if group_type is not None:
            current_type = group_type
            label_lines = []
            value_lines = []
            values = []
            continue
        if current_type is None:
            continue
        numeric = _data_values(line.text)
        if numeric is not None:
            if not label_lines:
                continue
            values.extend(numeric)
            value_lines.append(line)
            if len(values) >= 6:
                name = _normalize_dimension_name(
                    "".join(item.compact for item in label_lines)
                )
                if name:
                    grouped_rows.append(
                        (
                            current_type,
                            ParsedRow(
                                name=name,
                                values=tuple(values[:6]),
                                lines=tuple([*label_lines, *value_lines]),
                            ),
                        )
                    )
                label_lines = []
                value_lines = []
                values = []
            continue
        if _skip_table_line(line):
            if _resets_row_label(line):
                label_lines = []
                value_lines = []
                values = []
            continue
        if _looks_like_row_label(line.text):
            if values:
                label_lines = []
                value_lines = []
                values = []
            label_lines.append(line)

    money_scale, source_unit = _money_scale_near(lines, start_index)
    columns = (
        OutputColumn(
            0,
            "segment_revenue",
            "营业收入",
            "元",
            "annual",
            lambda value, scale=money_scale: value * scale,
            "CNY",
        ),
        OutputColumn(
            1,
            "segment_cost",
            "营业成本",
            "元",
            "annual",
            lambda value, scale=money_scale: value * scale,
            "CNY",
        ),
        OutputColumn(
            2,
            "segment_margin",
            "毛利率",
            "ratio",
            "annual",
            lambda value: value / 100.0,
        ),
    )
    result: list[DimensionalFactRecord] = []
    for dimension_type, row in grouped_rows:
        result.extend(
            _row_facts(
                document=document,
                snapshot_id=snapshot_id,
                period_start=period_start,
                period_end=period_end,
                dimension_type=dimension_type,
                parent_dimension="占营业收入或营业利润10%以上",
                table_title=heading,
                row=row,
                columns=columns,
                accounting_basis="company_disclosed_above_10_percent",
                metadata={
                    "source_unit": source_unit,
                    "reconciliation_scope": "partial",
                },
            )
        )
    return result


def _capacity_summary_facts(
    lines: list[IndexedLine],
    *,
    document: DocumentRecord,
    snapshot_id: str,
    period_start: date,
    period_end: date,
) -> list[DimensionalFactRecord]:
    heading = "不同产品或业务的产销情况"
    rows, _ = _fixed_width_rows(
        lines,
        heading=heading,
        end_headings=("公司实物销售收入是否大于劳务收入",),
        expected_values=4,
    )
    columns = (
        OutputColumn(
            0,
            "production_capacity",
            "产能",
            ROW_DISCLOSED_UNIT,
            "instant",
            float,
        ),
        OutputColumn(
            1,
            "under_construction_capacity",
            "在建产能",
            ROW_DISCLOSED_UNIT,
            "instant",
            float,
        ),
        OutputColumn(
            2,
            "capacity_utilization",
            "产能利用率",
            "ratio",
            "annual",
            lambda value: value / 100.0,
        ),
    )
    result: list[DimensionalFactRecord] = []
    for row in rows:
        name, unit = _split_embedded_unit(row.name)
        normalized = ParsedRow(
            name=name,
            values=row.values,
            lines=row.lines,
            unit=unit,
        )
        result.extend(
            _row_facts(
                document=document,
                snapshot_id=snapshot_id,
                period_start=period_start,
                period_end=period_end,
                dimension_type="capacity",
                parent_dimension="不同产品或业务的产销情况",
                table_title=heading,
                row=normalized,
                columns=columns,
                accounting_basis="company_disclosed_capacity_summary",
                metadata={"source_unit": unit},
            )
        )
    return result


def _physical_volume_facts(
    lines: list[IndexedLine],
    *,
    document: DocumentRecord,
    snapshot_id: str,
    period_start: date,
    period_end: date,
) -> list[DimensionalFactRecord]:
    heading = "公司实物销售收入是否大于劳务收入"
    start_index = _find_line(lines, heading)
    if start_index is None:
        return []
    end_index = len(lines)
    for end_heading in (
        "相关数据同比发生变动",
        "公司已签订的重大销售合同",
    ):
        candidate = _find_line(lines, end_heading, start=start_index + 1)
        if candidate is not None:
            end_index = min(end_index, candidate)

    metric_specs = {
        "生产量": ("production_volume", "annual"),
        "销售量": ("sales_volume", "annual"),
        "库存量": ("ending_inventory_volume", "instant"),
    }
    current_dimension: str | None = None
    dimension_line: IndexedLine | None = None
    pending_metric: tuple[str, str, str] | None = None
    metric_line: IndexedLine | None = None
    unit_line: IndexedLine | None = None
    row_unit: str | None = None
    result: list[DimensionalFactRecord] = []
    ignored = {"行业分类", "项目", "单位", "同比增减"}
    for line in lines[start_index + 1 : end_index]:
        compact = line.compact
        if compact in metric_specs:
            metric_id, period_type = metric_specs[compact]
            pending_metric = (metric_id, period_type, compact)
            metric_line = line
            unit_line = None
            row_unit = None
            continue
        if pending_metric is not None and _looks_like_disclosed_unit(line.text):
            row_unit = _normalize_disclosed_unit(line.text)
            unit_line = line
            continue
        numeric = _data_values(line.text)
        if pending_metric is not None and numeric is not None:
            if current_dimension is not None:
                metric_id, period_type, column_label = pending_metric
                evidence_lines = tuple(
                    item
                    for item in (
                        dimension_line,
                        metric_line,
                        unit_line,
                        line,
                    )
                    if item is not None
                )
                result.extend(
                    _row_facts(
                        document=document,
                        snapshot_id=snapshot_id,
                        period_start=period_start,
                        period_end=period_end,
                        dimension_type="product",
                        parent_dimension="实物产销情况",
                        table_title=heading,
                        row=ParsedRow(
                            name=current_dimension,
                            values=(numeric[0],),
                            lines=evidence_lines,
                            unit=row_unit,
                        ),
                        columns=(
                            OutputColumn(
                                0,
                                metric_id,
                                column_label,
                                ROW_DISCLOSED_UNIT,
                                period_type,
                                float,
                            ),
                        ),
                        accounting_basis="company_disclosed_physical_volume",
                        metadata={"source_unit": row_unit},
                    )
                )
            pending_metric = None
            metric_line = None
            unit_line = None
            row_unit = None
            continue
        if pending_metric is not None:
            continue
        if (
            compact
            and compact not in ignored
            and not re.fullmatch(r"20\d{2}年", compact)
            and not _skip_table_line(line)
            and _looks_like_row_label(line.text)
        ):
            current_dimension = _normalize_dimension_name(compact)
            dimension_line = line
    return result


def _split_embedded_unit(value: str) -> tuple[str, str | None]:
    compact = _compact(value)
    match = re.fullmatch(r"(?P<name>.+?)[（(](?P<unit>[^）)]+)[）)]", compact)
    if match and _looks_like_disclosed_unit(match.group("unit")):
        return (
            _normalize_dimension_name(match.group("name")),
            _normalize_disclosed_unit(match.group("unit")),
        )
    return _normalize_dimension_name(compact), None


def _skip_table_line(line: IndexedLine) -> bool:
    compact = line.compact
    if not compact or compact.startswith("---page"):
        return True
    if "年度报告" in compact and re.search(r"20\d{2}", compact):
        return True
    if re.fullmatch(r"\d+/\d+", compact):
        return True
    if any(token in compact for token in ("√适用", "□适用", "不适用")):
        return True
    if compact in {"吨", "元", "万元", "亿元", "单位", "币种：人民币", "币种:人民币"}:
        return True
    return any(fragment in compact for fragment in COMMON_HEADER_FRAGMENTS)


def _resets_row_label(line: IndexedLine) -> bool:
    """Return whether a skipped line is a header/boundary rather than row data.

    A unit such as ``吨`` can appear between a row label and its numeric values,
    so skipped lines cannot all clear the pending label.  Header fragments and
    page boundaries, however, must clear it to prevent split PDF headers from
    leaking into the first dimension name.
    """

    compact = line.compact
    if not compact:
        return False
    if compact in {"吨", "元", "万元", "亿元", "币种：人民币", "币种:人民币"}:
        return False
    if compact.startswith("---page"):
        return True
    if "年度报告" in compact and re.search(r"20\d{2}", compact):
        return True
    if re.fullmatch(r"\d+/\d+", compact):
        return True
    if any(token in compact for token in ("√适用", "□适用", "不适用")):
        return True
    if compact == "单位":
        return True
    return any(fragment in compact for fragment in COMMON_HEADER_FRAGMENTS)


def _looks_like_row_label(value: str) -> bool:
    compact = _compact(value)
    return bool(
        compact
        and len(compact) <= 60
        and re.search(r"[\u4e00-\u9fff]", compact)
        and not compact.startswith(("注：", "注:", "说明：", "说明:"))
    )


def _data_values(value: str) -> list[float] | None:
    normalized = re.sub(r"\s+", " ", value.strip()).replace("％", "%").replace("−", "-")
    compact = normalized.replace(" ", "")
    change = re.fullmatch(
        rf"(?P<direction>增加|减少)(?P<number>{NUMBER_PATTERN})(?:个百|个)?(?:分点)?",
        compact,
    )
    if change:
        number = _number(change.group("number"))
        return [-number if change.group("direction") == "减少" else number]
    mixed_change = re.fullmatch(
        rf"(?P<prefix>[-+\d,，.\s]+)(?P<direction>增加|减少)"
        rf"(?P<number>{NUMBER_PATTERN})(?:个百分点|个百分点|个百?分点)?",
        normalized,
    )
    if mixed_change:
        prefix = re.findall(NUMBER_PATTERN, mixed_change.group("prefix"))
        change_value = _number(mixed_change.group("number"))
        if mixed_change.group("direction") == "减少":
            change_value = -change_value
        return [*(_number(item) for item in prefix), change_value]
    candidate = normalized.replace("%", "").strip()
    if not re.fullmatch(r"[-+\d,，.\s]+", candidate):
        return None
    numbers = re.findall(NUMBER_PATTERN, candidate)
    return [_number(item) for item in numbers] if numbers else None


def _row_facts(
    *,
    document: DocumentRecord,
    snapshot_id: str,
    period_start: date,
    period_end: date,
    dimension_type: str,
    parent_dimension: str,
    table_title: str,
    row: ParsedRow,
    columns: tuple[OutputColumn, ...],
    accounting_basis: str,
    metadata: dict,
) -> list[DimensionalFactRecord]:
    result = []
    evidence_start = min(item.start for item in row.lines)
    evidence_end = max(item.end for item in row.lines)
    evidence_page = next((item.page for item in row.lines if _data_values(item.text)), None)
    quote = " ".join(item.text for item in row.lines if item.text)
    for column in columns:
        if column.index >= len(row.values):
            continue
        value = column.transform(row.values[column.index])
        unit = row.unit if column.unit == ROW_DISCLOSED_UNIT else column.unit
        unit_missing = not unit
        span = EvidenceSpan(
            document_id=document.document_id,
            page=evidence_page,
            text=quote,
            table_title=table_title,
            row_label=row.name,
            column_label=column.column_label,
            start_offset=evidence_start,
            end_offset=evidence_end,
        )
        fact = _dimensional_fact(
                document=document,
                snapshot_id=snapshot_id,
                metric_id=column.metric_id,
                dimension_type=dimension_type,
                dimension_name=row.name,
                parent_dimension=parent_dimension,
                value=value,
                unit=unit or "",
                currency=column.currency,
                period_start=(
                    period_start if column.period_type != "instant" else None
                ),
                period_end=period_end,
                period_type=column.period_type,
                document_page=evidence_page,
                table_title=table_title,
                row_label=row.name,
                column_label=column.column_label,
                extraction_method=ExtractionMethod.TABLE_PARSER,
                evidence_spans=[span],
                accounting_basis=accounting_basis,
                metadata={
                    **metadata,
                    "parser_version": PARSER_VERSION,
                    "raw_row_values": list(row.values),
                    "row_disclosed_unit": row.unit,
                    "unit_resolution": "missing" if unit_missing else "resolved",
                },
            )
        if unit_missing:
            fact = fact.model_copy(
                update={"verification_status": VerificationStatus.PENDING}
            )
        result.append(fact)
    return result


def _concentration_facts(
    text: str,
    *,
    document: DocumentRecord,
    snapshot_id: str,
    period_start: date,
    period_end: date,
) -> list[DimensionalFactRecord]:
    specs = (
        (
            "customer",
            "前五名客户合计",
            "top5_customer_sales",
            "customer_concentration",
            r"前\s*五\s*名\s*客户\s*销售额",
            r"年度\s*销售\s*总额",
            "销售额",
            r"前\s*五\s*名\s*客户\s*合计\s*销售\s*金额",
            r"前\s*五\s*名\s*客户\s*合计\s*销售\s*金额\s*占\s*年度\s*销售\s*总额\s*比例",
        ),
        (
            "supplier",
            "前五名供应商合计",
            "top5_supplier_procurement",
            "supplier_concentration",
            r"前\s*五\s*名\s*供应商\s*采购额",
            r"年度\s*采购\s*总额",
            "采购额",
            r"前\s*五\s*名\s*供应商\s*合计\s*采购\s*金额",
            r"前\s*五\s*名\s*供应商\s*合计\s*采购\s*金额\s*占\s*年度\s*采购\s*总额\s*比例",
        ),
    )
    result: list[DimensionalFactRecord] = []
    for (
        dimension_type,
        dimension_name,
        amount_metric,
        ratio_metric,
        label_pattern,
        denominator_pattern,
        amount_label,
        structured_amount_pattern,
        structured_ratio_pattern,
    ) in specs:
        pattern = re.compile(
            rf"{label_pattern}\s*(?P<amount>{NUMBER_PATTERN})\s*(?P<unit>亿元|万元|千元|元)"
            rf"[\s\S]{{0,45}}?占\s*{denominator_pattern}\s*(?P<ratio>{NUMBER_PATTERN})\s*%",
        )
        match = pattern.search(text)
        if match is None:
            pattern = re.compile(
                rf"{structured_amount_pattern}\s*[（(]\s*"
                rf"(?P<unit>亿元|万元|千元|元)\s*[）)]\s*"
                rf"(?P<amount>{NUMBER_PATTERN})"
                rf"[\s\S]{{0,160}}?{structured_ratio_pattern}\s*"
                rf"(?P<ratio>{NUMBER_PATTERN})\s*%",
            )
            match = pattern.search(text)
        if match is None:
            continue
        source_unit = match.group("unit")
        scale = {
            "元": 1.0,
            "千元": 1_000.0,
            "万元": 10_000.0,
            "亿元": 100_000_000.0,
        }[source_unit]
        amount = _number(match.group("amount")) * scale
        ratio = _number(match.group("ratio")) / 100.0
        page = _page_at_offset(text, match.start())
        quote = re.sub(r"\s+", " ", match.group(0)).strip()
        for metric_id, value, unit, column_label, disclosed_as in (
            (
                amount_metric,
                amount,
                "元",
                amount_label,
                DisclosureForm.ANONYMIZED,
            ),
            (
                ratio_metric,
                ratio,
                "ratio",
                "占年度总额比例",
                DisclosureForm.PERCENTAGE_ONLY,
            ),
        ):
            span = EvidenceSpan(
                document_id=document.document_id,
                page=page,
                text=quote,
                table_title="主要销售客户及主要供应商情况",
                row_label=dimension_name,
                column_label=column_label,
                start_offset=match.start(),
                end_offset=match.end(),
            )
            result.append(
                _dimensional_fact(
                    document=document,
                    snapshot_id=snapshot_id,
                    metric_id=metric_id,
                    dimension_type=dimension_type,
                    dimension_name=dimension_name,
                    parent_dimension="前五名汇总",
                    value=value,
                    unit=unit,
                    currency="CNY" if unit == "元" else "",
                    period_start=period_start,
                    period_end=period_end,
                    period_type="annual",
                    document_page=page,
                    table_title="主要销售客户及主要供应商情况",
                    row_label=dimension_name,
                    column_label=column_label,
                    extraction_method=ExtractionMethod.TEXT_RULE,
                    evidence_spans=[span],
                    accounting_basis="company_disclosed_top5_aggregate",
                    disclosed_as=disclosed_as,
                    metadata={
                        "parser_version": PARSER_VERSION,
                        "source_unit": source_unit,
                        "anonymous_aggregate": True,
                    },
                )
            )
    return result


def _dimensional_fact(
    *,
    document: DocumentRecord,
    snapshot_id: str,
    metric_id: str,
    dimension_type: str,
    dimension_name: str,
    parent_dimension: str,
    value: float,
    unit: str,
    currency: str,
    period_start: date | None,
    period_end: date,
    period_type: str,
    document_page: int | None,
    table_title: str,
    row_label: str,
    column_label: str,
    extraction_method: ExtractionMethod,
    evidence_spans: list[EvidenceSpan],
    accounting_basis: str,
    metadata: dict,
    disclosed_as: DisclosureForm = DisclosureForm.EXACT,
) -> DimensionalFactRecord:
    available_at = document.source.published_at or document.extracted_at
    identity_payload = {
        "snapshot_id": snapshot_id,
        "document_hash": document.sha256,
        "metric_id": metric_id,
        "dimension_type": dimension_type,
        "dimension_name": dimension_name,
        "period_end": period_end.isoformat(),
        "table_title": table_title,
        "column_label": column_label,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    dimension_digest = hashlib.sha256(
        f"{dimension_type}|{dimension_name}".encode("utf-8")
    ).hexdigest()[:16]
    return DimensionalFactRecord(
        dimensional_fact_id=f"dim-{document.ticker}-{digest[:24]}",
        ticker=document.ticker,
        metric_id=metric_id,
        dimension_type=dimension_type,
        dimension_name=dimension_name,
        dimension_code=f"{dimension_type}:{dimension_digest}",
        parent_dimension=parent_dimension,
        value=value,
        unit=unit,
        currency=currency,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
        available_at=available_at,
        scope="consolidated",
        accounting_basis=accounting_basis,
        audited=None,
        source_ids=[document.source.source_id],
        document_ids=[document.document_id],
        document_page=document_page,
        table_title=table_title,
        row_label=row_label,
        column_label=column_label,
        evidence_spans=evidence_spans,
        extraction_method=extraction_method,
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        data_snapshot_id=snapshot_id,
        disclosed_as=disclosed_as,
        metadata=metadata,
    )


def _validate_dimensional_facts(
    facts: list[DimensionalFactRecord],
) -> list[DimensionalFactRecord]:
    updated = list(facts)
    index = {
        (item.dimension_type, item.dimension_name, item.metric_id): position
        for position, item in enumerate(updated)
    }
    dimension_keys = {
        (item.dimension_type, item.dimension_name)
        for item in updated
        if item.metric_id in {"segment_revenue", "segment_cost", "segment_margin"}
    }
    for dimension_type, dimension_name in dimension_keys:
        revenue = _indexed_value(updated, index, dimension_type, dimension_name, "segment_revenue")
        cost = _indexed_value(updated, index, dimension_type, dimension_name, "segment_cost")
        margin_position = index.get((dimension_type, dimension_name, "segment_margin"))
        if revenue is None or cost is None or margin_position is None or revenue == 0:
            continue
        disclosed = float(updated[margin_position].value)
        computed = (revenue - cost) / revenue
        difference = abs(disclosed - computed)
        status = "consistent" if difference <= 0.0002 else "conflict"
        updated[margin_position] = _with_validation(
            updated[margin_position],
            "gross_margin_check",
            {
                "status": status,
                "computed": computed,
                "disclosed": disclosed,
                "absolute_difference": difference,
                "tolerance": 0.0002,
            },
            downgrade=status == "conflict",
        )

    for metric_id in ("segment_revenue", "segment_cost"):
        target = next(
            (
                item
                for item in updated
                if item.dimension_type == "business"
                and item.metric_id == metric_id
                and item.metadata.get("reconciliation_scope") == "complete"
            ),
            None,
        )
        if target is None or target.value is None:
            continue
        for dimension_type in ("product", "region", "channel"):
            positions = [
                position
                for position, item in enumerate(updated)
                if item.dimension_type == dimension_type
                and item.metric_id == metric_id
                and item.metadata.get("reconciliation_scope") == "complete"
            ]
            if not positions:
                continue
            group_sum = sum(float(updated[position].value) for position in positions)
            difference = group_sum - float(target.value)
            relative = abs(difference) / max(abs(float(target.value)), 1.0)
            status = "consistent" if relative <= 1e-8 else "conflict"
            validation = {
                "status": status,
                "target_dimension_type": "business",
                "target_dimension_name": target.dimension_name,
                "target_value": target.value,
                "group_sum": group_sum,
                "difference": difference,
                "relative_difference": relative,
                "tolerance": 1e-8,
            }
            for position in positions:
                updated[position] = _with_validation(
                    updated[position],
                    "table_reconciliation",
                    validation,
                    downgrade=status == "conflict",
                )
    return updated


def _indexed_value(
    facts: list[DimensionalFactRecord],
    index: dict[tuple[str, str, str], int],
    dimension_type: str,
    dimension_name: str,
    metric_id: str,
) -> float | None:
    position = index.get((dimension_type, dimension_name, metric_id))
    if position is None or facts[position].value is None:
        return None
    return float(facts[position].value)


def _with_validation(
    fact: DimensionalFactRecord,
    key: str,
    value: dict,
    *,
    downgrade: bool,
) -> DimensionalFactRecord:
    metadata = dict(fact.metadata)
    metadata[key] = value
    update = {"metadata": metadata}
    if downgrade:
        update["verification_status"] = VerificationStatus.PENDING
    return fact.model_copy(update=update)


def _money_scale_near(
    lines: list[IndexedLine],
    heading_index: int | None,
) -> tuple[float, str]:
    if heading_index is None:
        return 1.0, "元"
    start = max(0, heading_index - 3)
    end = min(len(lines), heading_index + 8)
    context = " ".join(line.text for line in lines[start:end])
    for unit, scale in (
        ("亿元", 100_000_000.0),
        ("万元", 10_000.0),
        ("千元", 1_000.0),
        ("元", 1.0),
    ):
        if re.search(rf"单位\s*[：:]?\s*{unit}", context):
            return scale, unit
    return 1.0, "元"


_DISCLOSED_UNIT_PATTERN = re.compile(
    r"(?:万|千|亿)?(?:"
    r"平方米|立方米|千瓦时|兆瓦时|吉瓦时|万千升|"
    r"GWh|MWh|kWh|GW|MW|kW|"
    r"吨|台|套|辆|件|只|个|户|人|升|箱|瓶|米"
    r")(?:[/／](?:年|月|日|台|套))?",
    re.IGNORECASE,
)


def _looks_like_disclosed_unit(value: str) -> bool:
    compact = _normalize_disclosed_unit(value)
    return bool(compact and _DISCLOSED_UNIT_PATTERN.fullmatch(compact))


def _normalize_disclosed_unit(value: str) -> str:
    return (
        _compact(value)
        .strip("：:；;，,。.'\"“”‘’（）()")
        .replace("／", "/")
    )


def _disclosed_unit_near(
    lines: list[IndexedLine],
    heading: IndexedLine | None,
) -> str | None:
    if heading is None:
        return None
    heading_index = next(
        (index for index, item in enumerate(lines) if item.start == heading.start),
        None,
    )
    if heading_index is None:
        return None
    context = " ".join(
        item.text
        for item in lines[max(0, heading_index - 3) : heading_index + 35]
    )
    pattern = re.compile(
        rf"(?:计量)?单位\s*(?:为|是|按|[：:])?\s*[“\"']?"
        rf"(?P<unit>{_DISCLOSED_UNIT_PATTERN.pattern})",
        re.IGNORECASE,
    )
    match = pattern.search(context)
    return _normalize_disclosed_unit(match.group("unit")) if match else None


def _page_at_offset(text: str, offset: int) -> int | None:
    page = None
    for marker in re.finditer(r"--- page (?P<page>\d+) ---", text[:offset]):
        page = int(marker.group("page"))
    return page


def _normalize_dimension_name(value: str) -> str:
    return re.sub(r"\s+", "", value).strip("：:；;，,")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip()


def _number(value: str) -> float:
    return float(value.replace(",", "").replace("，", "").replace("−", "-"))
