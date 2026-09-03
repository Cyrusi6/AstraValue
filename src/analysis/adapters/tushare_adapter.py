from __future__ import annotations

import os
from datetime import date, datetime, time as datetime_time, timedelta, timezone

from ..models import FactRecord, SourceRecord, SyncRequest, SyncResult, VerificationStatus
from .akshare_adapter import CHINA_TZ, _aware, _normalize_ticker, _parse_number, _row_date


class TushareProAdapter:
    """Optional second raw market-data vendor.

    The token is read only from ``TUSHARE_TOKEN`` and is never persisted in a
    source, fact, report or log record.
    """

    name = "tushare"

    def sync(self, ticker: str, options: SyncRequest | None = None) -> SyncResult:
        options = options or SyncRequest(providers=[self.name])
        token = os.getenv("TUSHARE_TOKEN", "").strip()
        if not token:
            raise RuntimeError("未配置TUSHARE_TOKEN，Tushare Pro可选行情源不可用")
        try:
            import tushare as ts
        except ImportError as exc:
            raise RuntimeError("未安装Tushare，请安装tushare可选依赖") from exc

        ticker = _normalize_ticker(ticker)
        suffix = "SH" if ticker.startswith(("5", "6", "9")) else "SZ"
        ts_code = f"{ticker}.{suffix}"
        cutoff = _aware(options.as_of)
        cutoff_china = cutoff.astimezone(CHINA_TZ)
        market_end = (
            cutoff_china.date()
            if cutoff_china.time() >= datetime_time(15, 0)
            else cutoff_china.date() - timedelta(days=1)
        )
        source = SourceRecord(
            name="Tushare Pro每日指标",
            source_type="public-adapter",
            upstream_source_id="tushare-pro-daily-basic",
            url="https://tushare.pro/document/2?doc_id=32",
            authority_level=4,
            notes="可选第二行情供应商；凭证仅从环境变量读取且不落盘",
        )
        pro = ts.pro_api(token)
        frame = pro.daily_basic(
            ts_code=ts_code,
            start_date=(market_end - timedelta(days=45)).strftime("%Y%m%d"),
            end_date=market_end.strftime("%Y%m%d"),
            fields="ts_code,trade_date,close,pe_ttm,pb",
        )
        if frame is None or frame.empty:
            return SyncResult(
                ticker=ticker,
                provider_results={self.name: "近45日暂无每日指标"},
                as_of=cutoff,
                sources=[source],
                warnings=["Tushare Pro近45日没有返回可用每日指标"],
            )
        rows = []
        for _, row in frame.iterrows():
            trade_date = _row_date(row, "trade_date")
            if trade_date is not None and trade_date <= market_end:
                rows.append((trade_date, row))
        if not rows:
            raise RuntimeError("Tushare Pro返回结果缺少有效交易日期")
        trade_date, row = max(rows, key=lambda item: item[0])
        market_at = datetime.combine(
            trade_date, datetime_time(15, 0), tzinfo=CHINA_TZ
        ).astimezone(timezone.utc)
        facts = []
        for column, metric_id, unit in (
            ("close", "market_price", "元"),
            ("pe_ttm", "pe_ttm", "x"),
            ("pb", "pb", "x"),
        ):
            if column not in row.index:
                continue
            value = _parse_number(row[column])
            if value is None:
                continue
            facts.append(
                FactRecord(
                    ticker=ticker,
                    metric_id=metric_id,
                    value=value,
                    unit=unit,
                    period_end=trade_date,
                    period_type="market_quote",
                    as_of=market_at,
                    source_ids=[source.source_id],
                    verification_status=VerificationStatus.PENDING,
                    metadata={
                        "provider": "tushare",
                        "upstream": "tushare-pro-daily-basic",
                        "trade_date": trade_date.isoformat(),
                        "ts_code": ts_code,
                    },
                )
            )
        return SyncResult(
            ticker=ticker,
            provider_results={self.name: f"success: {trade_date.isoformat()}"},
            as_of=cutoff,
            sources=[source],
            facts=facts,
            warnings=[] if facts else ["Tushare Pro每日指标没有可用数值"],
        )
