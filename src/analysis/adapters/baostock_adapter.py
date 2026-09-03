from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..filing_parser import derive_single_quarter_facts
from ..models import FactRecord, SourceRecord, SyncRequest, SyncResult, VerificationStatus
from .akshare_adapter import _selected_periods


CHINA_TZ = ZoneInfo("Asia/Shanghai")


class BaostockAdapter:
    name = "baostock"

    def sync(self, ticker: str, options: SyncRequest | None = None) -> SyncResult:
        options = options or SyncRequest(providers=[self.name])
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError("未安装BaoStock，请安装sources可选依赖") from exc
        digits = "".join(char for char in ticker if char.isdigit())
        if len(digits) != 6:
            raise ValueError("A股代码必须包含6位数字")
        prefix = "sh" if digits.startswith(("5", "6", "9")) else "sz"
        symbol = f"{prefix}.{digits}"
        cutoff = _aware(options.as_of)
        cutoff_date = cutoff.astimezone(CHINA_TZ).date()
        market_source = SourceRecord(
            name="BaoStock历史行情",
            source_type="public-adapter",
            upstream_source_id="baostock-market",
            url="http://baostock.com/",
            authority_level=4,
            notes="不复权收盘价、PE TTM与PB MRQ，用作外部行情复核",
        )
        profit_source = SourceRecord(
            name="BaoStock季度盈利数据",
            source_type="public-adapter",
            upstream_source_id="baostock-financial-profit",
            url="http://baostock.com/",
            authority_level=4,
            notes="query_profit_data累计净利润，用于独立复核并确定性转单季",
        )
        facts: list[FactRecord] = []
        sources: list[SourceRecord] = []
        messages: list[str] = []
        warnings: list[str] = []
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(login.error_msg)
        try:
            try:
                market_facts, trade_date = _query_market_facts(
                    bs, symbol, digits, cutoff_date, market_source
                )
                facts.extend(market_facts)
                if market_facts:
                    sources.append(market_source)
                    messages.append(f"行情成功: {trade_date.isoformat()}")
                else:
                    messages.append("近45日暂无可用交易行情")
                    warnings.append("BaoStock近45日暂无可用交易行情")
            except Exception as exc:
                message = f"行情失败: {exc}"
                messages.append(message)
                warnings.append(message)

            try:
                profit_facts = _query_profit_facts(
                    bs,
                    symbol,
                    digits,
                    cutoff,
                    cutoff_date,
                    options.annual_years,
                    options.single_quarters,
                    profit_source,
                )
                if profit_facts:
                    profit_facts = derive_single_quarter_facts(
                        profit_facts, options.single_quarters
                    )
                    facts.extend(profit_facts)
                    sources.append(profit_source)
                periods = {
                    item.period_end
                    for item in profit_facts
                    if item.metric_id == "net_income"
                    and item.period_type in {"annual", "cumulative"}
                }
                single_periods = {
                    item.period_end
                    for item in profit_facts
                    if item.metric_id == "net_income"
                    and item.period_type == "single_quarter"
                }
                messages.append(
                    f"季度净利润成功: {len(periods)}期，单季度{len(single_periods)}期"
                )
            except Exception as exc:
                message = f"季度净利润失败: {exc}"
                messages.append(message)
                warnings.append(message)
        finally:
            bs.logout()

        return SyncResult(
            ticker=digits,
            provider_results={self.name: "；".join(messages)},
            as_of=cutoff,
            sources=sources,
            facts=facts,
            warnings=warnings,
        )


def _query_market_facts(
    bs,
    symbol: str,
    ticker: str,
    cutoff_date: date,
    source: SourceRecord,
) -> tuple[list[FactRecord], date]:
    query = bs.query_history_k_data_plus(
        symbol,
        "date,close,peTTM,pbMRQ,tradestatus",
        start_date=(cutoff_date - timedelta(days=45)).isoformat(),
        end_date=cutoff_date.isoformat(),
        frequency="d",
        adjustflag="3",
    )
    if query.error_code != "0":
        raise RuntimeError(f"BaoStock行情查询失败: {query.error_msg}")
    rows = []
    while query.next():
        rows.append(query.get_row_data())
    rows = [row for row in rows if len(row) >= 5 and row[4] == "1"]
    if not rows:
        return [], cutoff_date
    row = rows[-1]
    trade_date = date.fromisoformat(row[0])
    market_at = datetime.combine(
        trade_date, time(15, 0), tzinfo=CHINA_TZ
    ).astimezone(timezone.utc)
    facts = []
    for index, (metric, unit) in {
        1: ("market_price", "元"),
        2: ("pe_ttm", "x"),
        3: ("pb", "x"),
    }.items():
        if row[index] not in {"", "nan"}:
            facts.append(
                FactRecord(
                    ticker=ticker,
                    metric_id=metric,
                    value=float(row[index]),
                    unit=unit,
                    period_end=trade_date,
                    period_type="market_quote",
                    as_of=market_at,
                    source_ids=[source.source_id],
                    verification_status=VerificationStatus.PENDING,
                    metadata={
                        "provider": "baostock",
                        "upstream": "baostock-market",
                        "adjustflag": "3",
                        "trade_date": trade_date.isoformat(),
                    },
                )
            )
    return facts, trade_date


def _query_profit_facts(
    bs,
    symbol: str,
    ticker: str,
    cutoff: datetime,
    cutoff_date: date,
    annual_years: int,
    single_quarters: int,
    source: SourceRecord,
) -> list[FactRecord]:
    rows_by_period: dict[date, tuple[float, datetime]] = {}
    start_year = cutoff_date.year - max(
        annual_years + 1,
        math.ceil(single_quarters / 4) + 2,
    )
    for year in range(start_year, cutoff_date.year + 1):
        for quarter in range(1, 5):
            if _quarter_end(year, quarter) > cutoff_date:
                continue
            query = bs.query_profit_data(code=symbol, year=year, quarter=quarter)
            if query.error_code != "0":
                raise RuntimeError(
                    f"BaoStock盈利查询失败 {year}Q{quarter}: {query.error_msg}"
                )
            while query.next():
                payload = dict(zip(query.fields, query.get_row_data()))
                stat_date = _parse_iso_date(payload.get("statDate"))
                pub_date = _parse_iso_date(payload.get("pubDate"))
                value = _finite_float(payload.get("netProfit"))
                if stat_date is None or pub_date is None or value is None:
                    continue
                available_at = datetime.combine(
                    pub_date, time.min, tzinfo=CHINA_TZ
                ).astimezone(timezone.utc)
                if stat_date > cutoff_date or available_at > cutoff:
                    continue
                previous = rows_by_period.get(stat_date)
                if previous is None or available_at > previous[1]:
                    rows_by_period[stat_date] = (value, available_at)

    selected = _selected_periods(
        sorted(rows_by_period), annual_years, single_quarters
    )
    facts = []
    for stat_date in selected:
        value, available_at = rows_by_period[stat_date]
        annual = (stat_date.month, stat_date.day) == (12, 31)
        facts.append(
            FactRecord(
                ticker=ticker,
                metric_id="net_income",
                value=value,
                unit="元",
                period_start=date(stat_date.year, 1, 1),
                period_end=stat_date,
                period_type="annual" if annual else "cumulative",
                disclosed_at=available_at,
                as_of=available_at,
                audited=annual,
                original_label="netProfit",
                source_ids=[source.source_id],
                verification_status=VerificationStatus.PENDING,
                restatement_version=available_at.astimezone(CHINA_TZ).date().isoformat(),
                metadata={
                    "provider": "baostock",
                    "upstream": "baostock-financial-profit",
                    "reported_period": stat_date.isoformat(),
                    "api": "query_profit_data",
                },
            )
        )
    return facts


def _quarter_end(year: int, quarter: int) -> date:
    return {
        1: date(year, 3, 31),
        2: date(year, 6, 30),
        3: date(year, 9, 30),
        4: date(year, 12, 31),
    }[quarter]


def _parse_iso_date(value) -> date | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _finite_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
