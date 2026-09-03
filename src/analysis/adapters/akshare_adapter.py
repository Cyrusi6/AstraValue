from __future__ import annotations

import math
import re
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..filing_parser import derive_single_quarter_facts
from ..models import FactRecord, SourceRecord, SyncRequest, SyncResult, VerificationStatus


CHINA_TZ = ZoneInfo("Asia/Shanghai")


class AkshareAdapter:
    name = "akshare"

    def sync(self, ticker: str, options: SyncRequest | None = None) -> SyncResult:
        options = options or SyncRequest(providers=[self.name])
        ticker = _normalize_ticker(ticker)
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("未安装AKShare，请安装sources可选依赖") from exc

        cutoff = _aware(options.as_of)
        cutoff_china = cutoff.astimezone(CHINA_TZ)
        cutoff_date = cutoff_china.date()
        sources: list[SourceRecord] = []
        facts: list[FactRecord] = []
        messages: list[str] = []
        warnings: list[str] = []
        company_name: str | None = None

        market_source = SourceRecord(
            name="AKShare-东方财富单股历史行情适配器",
            source_type="public-adapter",
            upstream_source_id="eastmoney-market",
            url="https://quote.eastmoney.com/",
            authority_level=4,
            notes="不复权日线收盘价，用于与BaoStock独立复核",
        )
        try:
            market_end = cutoff_date if cutoff_china.time() >= datetime_time(15, 0) else cutoff_date - timedelta(days=1)
            frame = ak.stock_zh_a_hist(
                symbol=ticker,
                period="daily",
                start_date=(market_end - timedelta(days=45)).strftime("%Y%m%d"),
                end_date=market_end.strftime("%Y%m%d"),
                adjust="",
            )
            market_rows = []
            for _, row in frame.iterrows():
                trade_date = _row_date(row, "日期")
                if trade_date is not None and trade_date <= market_end:
                    market_rows.append((trade_date, row))
            if not market_rows:
                raise RuntimeError(f"截止{market_end.isoformat()}近45日没有交易行情")
            trade_date, row = max(market_rows, key=lambda item: item[0])
            value = _parse_number(row["收盘"]) if "收盘" in row.index else None
            if value is None:
                raise RuntimeError("历史行情缺少有效收盘价")
            market_at = datetime.combine(trade_date, datetime_time(15, 0), tzinfo=CHINA_TZ).astimezone(timezone.utc)
            facts.append(
                FactRecord(
                    ticker=ticker,
                    metric_id="market_price",
                    value=value,
                    unit="元",
                    period_end=trade_date,
                    period_type="market_quote",
                    as_of=market_at,
                    source_ids=[market_source.source_id],
                    verification_status=VerificationStatus.PENDING,
                    original_label="收盘",
                    metadata={
                        "provider": "akshare",
                        "upstream": "eastmoney-market",
                        "adjust": "none",
                        "trade_date": trade_date.isoformat(),
                    },
                )
            )
            sources.append(market_source)
            messages.append(f"单股历史行情抓取成功: {trade_date.isoformat()}")
        except Exception as exc:  # External interfaces must degrade explicitly.
            message = f"行情抓取失败: {exc}"
            messages.append(message)
            warnings.append(message)

        statement_source = SourceRecord(
            name="AKShare-东方财富财务报表适配器",
            source_type="public-adapter",
            upstream_source_id="eastmoney-financial-statements",
            url="https://data.eastmoney.com/bbsj/",
            authority_level=4,
            notes="结构化财务数据仅用于独立复核，正式披露PDF为主源",
        )
        symbol = f"{'SH' if ticker.startswith(('5', '6', '9')) else 'SZ'}{ticker}"
        statement_jobs = (
            (
                "利润表",
                ak.stock_profit_sheet_by_report_em,
                {
                    "OPERATE_INCOME": ("revenue", "元"),
                    "OPERATE_COST": ("cost_of_revenue", "元"),
                    "NETPROFIT": ("net_income", "元"),
                    "PARENT_NETPROFIT": ("net_income_parent", "元"),
                    "DEDUCT_PARENT_NETPROFIT": ("net_income_excl", "元"),
                    "SALE_EXPENSE": ("selling_expense", "元"),
                    "MANAGE_EXPENSE": ("administrative_expense", "元"),
                    "RESEARCH_EXPENSE": ("rd_expense", "元"),
                    "FINANCE_EXPENSE": ("finance_expense", "元"),
                    "MINORITY_INTEREST": ("minority_interest", "元"),
                    "ASSET_IMPAIRMENT_LOSS": ("asset_impairment_loss", "元"),
                },
                "flow",
            ),
            (
                "资产负债表",
                ak.stock_balance_sheet_by_report_em,
                {
                    "MONETARYFUNDS": ("cash", "元"),
                    "ACCOUNTS_RECE": ("accounts_receivable", "元"),
                    "INVENTORY": ("inventory", "元"),
                    "GOODWILL": ("goodwill", "元"),
                    "TOTAL_CURRENT_ASSETS": ("current_assets", "元"),
                    "TOTAL_CURRENT_LIAB": ("current_liabilities", "元"),
                    "TOTAL_ASSETS": ("total_assets", "元"),
                    "TOTAL_LIABILITIES": ("total_liabilities", "元"),
                    "TOTAL_EQUITY": ("total_equity", "元"),
                    "TOTAL_PARENT_EQUITY": ("total_parent_equity", "元"),
                    "ACCOUNTS_PAYABLE": ("accounts_payable", "元"),
                    "CONTRACT_ASSET": ("contract_assets", "元"),
                    "SHARE_CAPITAL": ("shares_outstanding", "股"),
                },
                "balance",
            ),
            (
                "现金流量表",
                ak.stock_cash_flow_sheet_by_report_em,
                {
                    "NETCASH_OPERATE": ("operating_cash_flow", "元"),
                    "NETCASH_INVEST": ("investing_cash_flow", "元"),
                    "NETCASH_FINANCE": ("financing_cash_flow", "元"),
                    "CONSTRUCT_LONG_ASSET": ("capital_expenditure", "元"),
                    "RATE_CHANGE_EFFECT": ("fx_effect", "元"),
                    "BEGIN_CCE": ("opening_cash", "元"),
                    "END_CCE": ("closing_cash", "元"),
                },
                "cashflow",
            ),
        )
        statement_facts: list[FactRecord] = []
        availability_by_period: dict[date, datetime] = {}
        for label, loader, mapping, statement_kind in statement_jobs:
            try:
                frame = loader(symbol=symbol)
                if frame is None or frame.empty:
                    raise RuntimeError("返回空表")
                rows = _statement_rows(
                    frame,
                    cutoff_date,
                    options.annual_years,
                    options.single_quarters,
                )
                emitted = _emit_statement_facts(
                    ticker,
                    rows,
                    mapping,
                    statement_kind,
                    statement_source,
                )
                if statement_kind == "balance":
                    emitted.extend(_emit_interest_bearing_debt(ticker, rows, statement_source))
                statement_facts.extend(emitted)
                for report_date, _, _, available_at in rows:
                    availability_by_period[report_date] = max(
                        availability_by_period.get(report_date, available_at), available_at
                    )
                if company_name is None:
                    company_name = _company_name(frame)
                messages.append(f"{label}抓取成功: {len(rows)}期/{len(emitted)}条事实")
            except Exception as exc:
                message = f"{label}抓取失败: {exc}"
                messages.append(message)
                warnings.append(message)

        if statement_facts:
            statement_facts = derive_single_quarter_facts(statement_facts, options.single_quarters)
            sources.append(statement_source)
            facts.extend(statement_facts)

        # THS contributes reported EPS/ROE and remains a separate upstream. Core
        # statement values come from the raw-yuan Eastmoney tables above.
        summary_source = SourceRecord(
            name="AKShare-同花顺财务摘要适配器",
            source_type="public-adapter",
            upstream_source_id="ths-financial-summary",
            url="https://basic.10jqka.com.cn/",
            authority_level=4,
            notes="摘要数据仅用于复核与补充，正式报告仍以交易所披露为主源",
        )
        try:
            frame = ak.stock_financial_abstract_ths(symbol=ticker, indicator="按报告期")
            if frame is None or frame.empty:
                raise RuntimeError("财务摘要为空")
            rows = _financial_rows(frame, cutoff_date, options.annual_years, options.single_quarters)
            summary_facts: list[FactRecord] = []
            mapping = {
                "基本每股收益": ("eps_basic", "元/股"),
                "净资产收益率": ("roe", "ratio"),
            }
            for report_date, row in rows:
                available_at = availability_by_period.get(report_date)
                if available_at is None:
                    available_at = datetime.combine(
                        _conservative_disclosure_date(report_date), datetime_time.min, tzinfo=CHINA_TZ
                    ).astimezone(timezone.utc)
                if available_at > cutoff:
                    continue
                for original_label, (metric_id, unit) in mapping.items():
                    if original_label not in row.index:
                        continue
                    value = _parse_number(row[original_label], percentage=(unit == "ratio"))
                    if value is None:
                        continue
                    summary_facts.append(
                        FactRecord(
                            ticker=ticker,
                            metric_id=metric_id,
                            value=value,
                            unit=unit,
                            period_end=report_date,
                            period_type="reported",
                            disclosed_at=available_at,
                            as_of=available_at,
                            audited=(report_date.month, report_date.day) == (12, 31),
                            source_ids=[summary_source.source_id],
                            verification_status=VerificationStatus.PENDING,
                            original_label=original_label,
                            metadata={
                                "provider": "akshare",
                                "upstream": "ths-financial-summary",
                                "reported_period": report_date.isoformat(),
                                "availability": "exact-from-eastmoney"
                                if report_date in availability_by_period
                                else "conservative-statutory-deadline",
                            },
                        )
                    )
            if summary_facts:
                sources.append(summary_source)
                facts.extend(summary_facts)
            messages.append(f"财务摘要抓取成功: {len(summary_facts)}条补充事实")
        except Exception as exc:
            message = f"财务摘要抓取失败: {exc}"
            messages.append(message)
            warnings.append(message)

        unique_sources = {item.source_id: item for item in sources}
        annual_periods = {
            item.period_end
            for item in facts
            if item.metric_id == "revenue" and item.period_type == "annual"
        }
        quarter_periods = {
            item.period_end
            for item in facts
            if item.metric_id == "revenue" and item.period_type == "single_quarter"
        }
        messages.append(f"覆盖汇总: {len(annual_periods)}期年度、{len(quarter_periods)}个单季度")
        return SyncResult(
            ticker=ticker,
            company_name=company_name,
            provider_results={self.name: "；".join(messages)},
            as_of=cutoff,
            sources=list(unique_sources.values()),
            facts=facts,
            warnings=warnings,
        )


def _emit_statement_facts(
    ticker: str,
    rows: list[tuple[date, Any, datetime | None, datetime]],
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
            if statement_kind == "balance" or metric_id in {"opening_cash", "closing_cash"}:
                period_type = "instant"
                period_start = None
            else:
                period_type = "annual" if annual else "cumulative"
                period_start = date(report_date.year, 1, 1)
            update_date = available_at.astimezone(CHINA_TZ).date()
            notice_date = notice_at.astimezone(CHINA_TZ).date() if notice_at else None
            is_restated = notice_date is not None and update_date > notice_date
            facts.append(
                FactRecord(
                    ticker=ticker,
                    metric_id=metric_id,
                    value=value,
                    unit=unit,
                    period_start=period_start,
                    period_end=report_date,
                    period_type=period_type,
                    disclosed_at=available_at,
                    as_of=available_at,
                    audited=annual,
                    original_label=column,
                    source_ids=[source.source_id],
                    verification_status=VerificationStatus.PENDING,
                    restatement_version=update_date.isoformat(),
                    is_restated=is_restated,
                    metadata={
                        "provider": "akshare",
                        "upstream": "eastmoney-financial-statements",
                        "statement": statement_kind,
                        "notice_date": notice_date.isoformat() if notice_date else None,
                        "update_date": update_date.isoformat(),
                        "reported_period": report_date.isoformat(),
                    },
                )
            )
    return facts


def _emit_interest_bearing_debt(
    ticker: str,
    rows: list[tuple[date, Any, datetime | None, datetime]],
    source: SourceRecord,
) -> list[FactRecord]:
    columns = ("SHORT_LOAN", "NONCURRENT_LIAB_1YEAR", "LONG_LOAN", "BOND_PAYABLE", "LEASE_LIAB")
    facts = []
    for report_date, row, notice_at, available_at in rows:
        components = {column: _parse_number(row[column]) for column in columns if column in row.index}
        components = {key: value for key, value in components.items() if value is not None}
        if not components:
            continue
        update_date = available_at.astimezone(CHINA_TZ).date()
        notice_date = notice_at.astimezone(CHINA_TZ).date() if notice_at else None
        facts.append(
            FactRecord(
                ticker=ticker,
                metric_id="interest_bearing_debt",
                value=sum(components.values()),
                unit="元",
                period_end=report_date,
                period_type="instant",
                disclosed_at=available_at,
                as_of=available_at,
                audited=(report_date.month, report_date.day) == (12, 31),
                original_label="+".join(components),
                source_ids=[source.source_id],
                verification_status=VerificationStatus.PENDING,
                restatement_version=update_date.isoformat(),
                is_restated=notice_date is not None and update_date > notice_date,
                metadata={
                    "provider": "akshare",
                    "upstream": "eastmoney-financial-statements",
                    "statement": "balance",
                    "components": components,
                },
            )
        )
    return facts


def _statement_rows(
    frame,
    cutoff: date,
    annual_years: int,
    single_quarters: int,
) -> list[tuple[date, Any, datetime | None, datetime]]:
    by_period: dict[date, tuple[Any, datetime | None, datetime]] = {}
    for _, row in frame.iterrows():
        report_date = _extract_report_date(row)
        if report_date is None or report_date > cutoff:
            continue
        notice_date = _row_date(row, "NOTICE_DATE")
        update_date = _row_date(row, "UPDATE_DATE") or notice_date
        if notice_date is None:
            notice_date = _conservative_disclosure_date(report_date)
        if update_date is None:
            update_date = notice_date
        if notice_date > cutoff or update_date > cutoff:
            continue
        notice_at = datetime.combine(notice_date, datetime_time.min, tzinfo=CHINA_TZ).astimezone(timezone.utc)
        available_at = datetime.combine(update_date, datetime_time.min, tzinfo=CHINA_TZ).astimezone(timezone.utc)
        previous = by_period.get(report_date)
        if previous is None or available_at > previous[2]:
            by_period[report_date] = (row, notice_at, available_at)
    selected_dates = _selected_periods(sorted(by_period), annual_years, single_quarters)
    return [
        (report_date, by_period[report_date][0], by_period[report_date][1], by_period[report_date][2])
        for report_date in selected_dates
    ]


def _financial_rows(frame, cutoff: date, annual_years: int, single_quarters: int):
    dated_rows = []
    for _, row in frame.iterrows():
        report_date = _extract_report_date(row)
        if report_date is not None and report_date <= cutoff:
            dated_rows.append((report_date, row))
    if not dated_rows:
        raise RuntimeError(f"财务摘要没有截止{cutoff.isoformat()}的报告期")
    by_period = {report_date: row for report_date, row in dated_rows}
    selected_dates = _selected_periods(sorted(by_period), annual_years, single_quarters)
    return [(report_date, by_period[report_date]) for report_date in selected_dates]


def _selected_periods(
    report_dates: list[date], annual_years: int, single_quarters: int
) -> list[date]:
    if not report_dates:
        return []
    annual_dates = [item for item in report_dates if (item.month, item.day) == (12, 31)]
    keep_annual = set(annual_dates[-annual_years:])
    latest_year = report_dates[-1].year
    interim_years = math.ceil(single_quarters / 4) + 1
    return [
        item
        for item in report_dates
        if item in keep_annual or item.year >= latest_year - interim_years + 1
    ]


def _normalize_ticker(ticker: str) -> str:
    digits = "".join(char for char in ticker if char.isdigit())
    if len(digits) != 6:
        raise ValueError("A股代码必须包含6位数字")
    return digits


def _find_column(columns, candidates: list[str]) -> str:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    raise RuntimeError(f"未找到列: {candidates}")


def _parse_number(value: Any, *, percentage: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number / 100 if percentage else number
    text = str(value).strip().replace(",", "")
    if text.lower() in {"", "--", "-", "nan", "none", "nat"}:
        return None
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier, text = 100_000_000.0, text[:-1]
    elif text.endswith("万"):
        multiplier, text = 10_000.0, text[:-1]
    had_percent_sign = text.endswith("%")
    if had_percent_sign:
        text = text[:-1]
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group()) * multiplier
    return number / 100 if percentage or had_percent_sign else number


def _extract_report_date(row) -> date | None:
    for label in ("REPORT_DATE", "报告期", "报告日期", "日期"):
        parsed = _row_date(row, label)
        if parsed is not None:
            return parsed
    return None


def _row_date(row, label: str) -> date | None:
    if label not in row.index:
        return None
    value = row[label]
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except (ValueError, TypeError):
            pass
    text = str(value).strip()
    if text.lower() in {"", "nan", "nat", "none"}:
        return None
    match = re.search(r"(20\d{2})[-年/]?(\d{1,2})[-月/]?(\d{1,2})", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    return None


def _company_name(frame) -> str | None:
    if "SECURITY_NAME_ABBR" not in frame.columns or frame.empty:
        return None
    value = str(frame.iloc[0]["SECURITY_NAME_ABBR"]).strip()
    return value if value and value.lower() not in {"nan", "none"} else None


def _conservative_disclosure_date(report_date: date) -> date:
    if (report_date.month, report_date.day) == (12, 31):
        return date(report_date.year + 1, 4, 30)
    if (report_date.month, report_date.day) == (3, 31):
        return date(report_date.year, 4, 30)
    if (report_date.month, report_date.day) == (6, 30):
        return date(report_date.year, 8, 31)
    return date(report_date.year, 10, 31)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
