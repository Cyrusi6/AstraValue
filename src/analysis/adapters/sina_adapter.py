from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any

from ..filing_parser import derive_single_quarter_facts
from ..models import FactRecord, SourceRecord, SyncRequest, SyncResult, VerificationStatus
from .akshare_adapter import (
    CHINA_TZ,
    _aware,
    _conservative_disclosure_date,
    _normalize_ticker,
    _parse_number,
    _row_date,
    _selected_periods,
)


STATEMENT_JOBS: tuple[tuple[str, dict[str, tuple[str, str]], str], ...] = (
    (
        "利润表",
        {
            "营业收入": ("revenue", "元"),
            "营业成本": ("cost_of_revenue", "元"),
            "净利润": ("net_income", "元"),
            "归属于母公司所有者的净利润": ("net_income_parent", "元"),
            "销售费用": ("selling_expense", "元"),
            "管理费用": ("administrative_expense", "元"),
            "研发费用": ("rd_expense", "元"),
            "财务费用": ("finance_expense", "元"),
            "少数股东损益": ("minority_interest", "元"),
            "资产减值损失": ("asset_impairment_loss", "元"),
        },
        "income",
    ),
    (
        "资产负债表",
        {
            "货币资金": ("cash", "元"),
            "应收账款": ("accounts_receivable", "元"),
            "存货": ("inventory", "元"),
            "商誉": ("goodwill", "元"),
            "流动资产合计": ("current_assets", "元"),
            "流动负债合计": ("current_liabilities", "元"),
            "资产总计": ("total_assets", "元"),
            "负债合计": ("total_liabilities", "元"),
            "所有者权益(或股东权益)合计": ("total_equity", "元"),
            "归属于母公司股东权益合计": ("total_parent_equity", "元"),
            "应付账款": ("accounts_payable", "元"),
            "合同资产": ("contract_assets", "元"),
            "实收资本(或股本)": ("shares_outstanding", "股"),
        },
        "balance",
    ),
    (
        "现金流量表",
        {
            "经营活动产生的现金流量净额": ("operating_cash_flow", "元"),
            "投资活动产生的现金流量净额": ("investing_cash_flow", "元"),
            "筹资活动产生的现金流量净额": ("financing_cash_flow", "元"),
            "购建固定资产、无形资产和其他长期资产所支付的现金": (
                "capital_expenditure",
                "元",
            ),
            "汇率变动对现金及现金等价物的影响": ("fx_effect", "元"),
            "期初现金及现金等价物余额": ("opening_cash", "元"),
            "期末现金及现金等价物余额": ("closing_cash", "元"),
        },
        "cashflow",
    ),
)


class SinaFinanceAdapter:
    """Independent structured statement channel backed by Sina Finance."""

    name = "sina"

    def sync(self, ticker: str, options: SyncRequest | None = None) -> SyncResult:
        options = options or SyncRequest(providers=[self.name])
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("未安装AKShare，请安装sources可选依赖") from exc

        ticker = _normalize_ticker(ticker)
        stock = f"{'sh' if ticker.startswith(('5', '6', '9')) else 'sz'}{ticker}"
        cutoff = _aware(options.as_of)
        cutoff_china = cutoff.astimezone(CHINA_TZ)
        sources: list[SourceRecord] = []
        market_source = SourceRecord(
            name="AKShare-新浪财经历史行情适配器",
            source_type="public-adapter",
            upstream_source_id="sina-market",
            url="https://finance.sina.com.cn/realstock/",
            authority_level=4,
            notes="新浪不复权日线收盘价；可在东方财富行情不可用时提供独立价格值",
        )
        financial_source = SourceRecord(
            name="AKShare-新浪财经财务报表适配器",
            source_type="public-adapter",
            upstream_source_id="sina-financial-statements",
            url="https://money.finance.sina.com.cn/",
            authority_level=4,
            notes="新浪结构化财务报表用于交叉复核；正式披露文件仍为权威主源",
        )
        facts: list[FactRecord] = []
        messages: list[str] = []
        warnings: list[str] = []

        try:
            market_end = (
                cutoff_china.date()
                if cutoff_china.time() >= datetime_time(15, 0)
                else cutoff_china.date() - timedelta(days=1)
            )
            frame = ak.stock_zh_a_daily(
                symbol=stock,
                start_date=(market_end - timedelta(days=45)).strftime("%Y%m%d"),
                end_date=market_end.strftime("%Y%m%d"),
                adjust="",
            )
            market_fact = _sina_market_fact(
                frame,
                ticker,
                market_end,
                market_source,
            )
            facts.append(market_fact)
            sources.append(market_source)
            messages.append(f"历史行情成功: {market_fact.period_end.isoformat()}")
        except Exception as exc:
            message = f"历史行情失败: {exc}"
            messages.append(message)
            warnings.append(message)

        statement_facts: list[FactRecord] = []
        for label, mapping, statement_kind in STATEMENT_JOBS:
            try:
                frame = ak.stock_financial_report_sina(stock=stock, symbol=label)
                if frame is None or frame.empty:
                    raise RuntimeError("返回空表")
                rows = _sina_statement_rows(
                    frame,
                    cutoff,
                    options.annual_years,
                    options.single_quarters,
                )
                emitted = _emit_sina_facts(
                    ticker, rows, mapping, statement_kind, financial_source
                )
                statement_facts.extend(emitted)
                messages.append(f"{label}成功: {len(rows)}期/{len(emitted)}条事实")
            except Exception as exc:
                message = f"{label}失败: {exc}"
                messages.append(message)
                warnings.append(message)

        if statement_facts:
            statement_facts = derive_single_quarter_facts(
                statement_facts, options.single_quarters
            )
            facts.extend(statement_facts)
            sources.append(financial_source)
        messages.append(f"结构化事实合计: {len(facts)}条")
        return SyncResult(
            ticker=ticker,
            provider_results={self.name: "；".join(messages)},
            as_of=cutoff,
            sources=sources,
            facts=facts,
            warnings=warnings,
        )


def _sina_market_fact(
    frame,
    ticker: str,
    cutoff: date,
    source: SourceRecord,
) -> FactRecord:
    if frame is None or frame.empty:
        raise RuntimeError("返回空表")
    rows = []
    for _, row in frame.iterrows():
        trade_date = _row_date(row, "date")
        if trade_date is not None and trade_date <= cutoff:
            rows.append((trade_date, row))
    if not rows:
        raise RuntimeError(f"截止{cutoff.isoformat()}近45日没有交易行情")
    trade_date, row = max(rows, key=lambda item: item[0])
    value = _parse_number(row["close"]) if "close" in row.index else None
    if value is None or value <= 0:
        raise RuntimeError("历史行情缺少有效收盘价")
    market_at = datetime.combine(
        trade_date, datetime_time(15, 0), tzinfo=CHINA_TZ
    ).astimezone(timezone.utc)
    return FactRecord(
        ticker=ticker,
        metric_id="market_price",
        value=value,
        unit="元",
        period_end=trade_date,
        period_type="market_quote",
        as_of=market_at,
        source_ids=[source.source_id],
        verification_status=VerificationStatus.PENDING,
        original_label="close",
        metadata={
            "provider": "sina",
            "upstream": "sina-market",
            "adjust": "none",
            "trade_date": trade_date.isoformat(),
        },
    )


def _sina_statement_rows(
    frame,
    cutoff: datetime,
    annual_years: int,
    single_quarters: int,
) -> list[tuple[date, Any, datetime, datetime]]:
    cutoff_date = cutoff.astimezone(CHINA_TZ).date()
    by_period: dict[date, tuple[Any, datetime, datetime]] = {}
    for _, row in frame.iterrows():
        row_type = str(row["类型"]).strip() if "类型" in row.index else ""
        if row_type and "合并" not in row_type:
            continue
        report_date = _row_date(row, "报告日")
        if report_date is None or report_date > cutoff_date:
            continue
        notice_date = _row_date(row, "公告日期") or _conservative_disclosure_date(report_date)
        update_date = _row_date(row, "更新日期") or notice_date
        available_date = max(notice_date, update_date)
        if available_date > cutoff_date:
            continue
        notice_at = _china_midnight(notice_date)
        available_at = _china_midnight(available_date)
        previous = by_period.get(report_date)
        if previous is None or available_at > previous[2]:
            by_period[report_date] = (row, notice_at, available_at)
    selected = _selected_periods(sorted(by_period), annual_years, single_quarters)
    return [
        (period, by_period[period][0], by_period[period][1], by_period[period][2])
        for period in selected
    ]


def _emit_sina_facts(
    ticker: str,
    rows: list[tuple[date, Any, datetime, datetime]],
    mapping: dict[str, tuple[str, str]],
    statement_kind: str,
    source: SourceRecord,
) -> list[FactRecord]:
    facts: list[FactRecord] = []
    for report_date, row, notice_at, available_at in rows:
        annual = (report_date.month, report_date.day) == (12, 31)
        for column, (metric_id, unit) in mapping.items():
            if column not in row.index:
                continue
            value = _parse_number(row[column])
            if value is None:
                continue
            instant = statement_kind == "balance" or metric_id in {"opening_cash", "closing_cash"}
            facts.append(
                FactRecord(
                    ticker=ticker,
                    metric_id=metric_id,
                    value=value,
                    unit=unit,
                    period_start=None if instant else date(report_date.year, 1, 1),
                    period_end=report_date,
                    period_type="instant" if instant else ("annual" if annual else "cumulative"),
                    disclosed_at=available_at,
                    as_of=available_at,
                    audited=annual,
                    original_label=column,
                    source_ids=[source.source_id],
                    verification_status=VerificationStatus.PENDING,
                    restatement_version=available_at.astimezone(CHINA_TZ).date().isoformat(),
                    is_restated=available_at > notice_at,
                    metadata={
                        "provider": "sina",
                        "upstream": "sina-financial-statements",
                        "statement": statement_kind,
                        "notice_date": notice_at.astimezone(CHINA_TZ).date().isoformat(),
                        "update_date": available_at.astimezone(CHINA_TZ).date().isoformat(),
                        "reported_period": report_date.isoformat(),
                    },
                )
            )
    return facts


def _china_midnight(value: date) -> datetime:
    return datetime.combine(value, datetime_time.min, tzinfo=CHINA_TZ).astimezone(timezone.utc)
