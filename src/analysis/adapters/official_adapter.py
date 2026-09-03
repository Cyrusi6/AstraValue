from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..announcement_index import (
    AnnouncementCandidate,
    attach_document_and_event,
    build_announcement_record,
    candidate_download_order,
    merge_announcement_candidates,
)
from ..documents import RAW_ROOT, ingest_downloaded_document
from ..dimensional_parser import parse_dimensional_facts
from ..event_state import EventClassification
from ..event_linking import link_event_series
from ..event_parser import enrich_event_from_document
from ..filing_parser import FilingPeriod, derive_single_quarter_facts, detect_filing_period, parse_official_document
from ..models import DimensionalFactRecord, FactRecord, SyncRequest, SyncResult, new_id


CNINFO_STOCK_LIST = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
SSE_QUERY = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
SZSE_QUERY = "https://www.szse.cn/api/disc/announcement/annList"
CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FilingCandidate:
    ticker: str
    company_name: str
    title: str
    published_at: datetime
    url: str
    provider: str
    announcement_id: str
    period: FilingPeriod
    metadata: dict[str, Any]

    @property
    def key(self) -> tuple[int, str]:
        return self.period.year, self.period.kind


class OfficialDisclosureAdapter:
    name = "official"

    def __init__(self, raw_root: Path | str = RAW_ROOT) -> None:
        self.raw_root = Path(raw_root)

    def sync(self, ticker: str, options: SyncRequest | None = None) -> SyncResult:
        options = options or SyncRequest(providers=[self.name])
        ticker = _normalize_ticker(ticker)
        as_of = _aware(options.as_of)
        sync_result_id = new_id()
        requested_scopes = set(options.scopes)
        wants_financials = "financials" in requested_scopes
        wants_dimensions = "dimensions" in requested_scopes
        wants_periodic_reports = wants_financials or wants_dimensions
        wants_announcements = bool(
            {"announcements", "governance", "capital_actions", "risks"}
            & requested_scopes
        )
        if not wants_periodic_reports:
            result = SyncResult(
                sync_result_id=sync_result_id,
                ticker=ticker,
                scopes=options.scopes,
                provider_results={
                    self.name: (
                        "当前同步范围不包含定期报告"
                        if wants_announcements
                        else "当前同步范围没有正式披露任务"
                    )
                },
                as_of=as_of,
            )
            return (
                self._attach_announcement_index(result, ticker, options)
                if wants_announcements
                else result
            )
        as_of_china = as_of.astimezone(CHINA_TZ)
        selected_single_quarters = options.single_quarters if wants_financials else 0
        start_year = min(
            as_of_china.year - options.annual_years - 1,
            as_of_china.year - math.ceil(selected_single_quarters / 4) - 1,
        )
        start_date = date(start_year, 1, 1)
        end_date = as_of_china.date()
        timeout = options.request_timeout_seconds
        candidates: list[FilingCandidate] = []
        warnings: list[str] = []
        provider_results: dict[str, str] = {}

        try:
            cninfo = _CninfoClient(timeout).list_filings(ticker, start_date, end_date, as_of)
            candidates.extend(cninfo)
            provider_results["cninfo"] = f"元数据成功: {len(cninfo)}份定期报告"
        except Exception as exc:
            provider_results["cninfo"] = f"元数据失败: {exc}"
            warnings.append(f"巨潮元数据失败: {exc}")

        exchange_name = "sse" if _is_shanghai(ticker) else "szse"
        try:
            if exchange_name == "sse":
                exchange = _SseClient(timeout).list_filings(ticker, start_date, end_date, as_of)
            else:
                exchange = _SzseClient(timeout).list_filings(ticker, start_date, end_date, as_of)
            candidates.extend(exchange)
            provider_results[exchange_name] = f"元数据成功: {len(exchange)}份定期报告"
        except Exception as exc:
            provider_results[exchange_name] = f"元数据失败: {exc}"
            warnings.append(f"{exchange_name.upper()}元数据失败: {exc}")

        selected = _select_filings(
            candidates,
            options.annual_years,
            selected_single_quarters,
        )
        if not selected:
            provider_results[self.name] = "未找到截止时点前可用的完整定期报告"
            return SyncResult(
                sync_result_id=sync_result_id,
                ticker=ticker,
                scopes=options.scopes,
                provider_results=provider_results,
                as_of=as_of,
                warnings=warnings or ["正式披露源没有返回可用定期报告"],
            ) if not wants_announcements else self._attach_announcement_index(
                SyncResult(
                    sync_result_id=sync_result_id,
                    ticker=ticker,
                    scopes=options.scopes,
                    provider_results=provider_results,
                    as_of=as_of,
                    warnings=warnings or ["正式披露源没有返回可用定期报告"],
                ),
                ticker,
                options,
            )
        company_name = selected[0].company_name
        if not options.download_official_documents:
            provider_results[self.name] = f"仅校验元数据: {len(selected)}份，未下载PDF"
            return SyncResult(
                sync_result_id=sync_result_id,
                ticker=ticker,
                scopes=options.scopes,
                company_name=company_name,
                provider_results=provider_results,
                as_of=as_of,
                warnings=warnings,
            ) if not wants_announcements else self._attach_announcement_index(
                SyncResult(
                    sync_result_id=sync_result_id,
                    ticker=ticker,
                    scopes=options.scopes,
                    company_name=company_name,
                    provider_results=provider_results,
                    as_of=as_of,
                    warnings=warnings,
                ),
                ticker,
                options,
            )

        documents = []
        facts: list[FactRecord] = []
        dimensional_facts: list[DimensionalFactRecord] = []
        fallback_count = 0
        with _http_client(timeout, "https://www.cninfo.com.cn/") as client:
            for candidate in selected:
                failures = []
                for attempt in _candidate_attempts(candidate, candidates):
                    try:
                        content = _download_pdf(
                            client,
                            attempt.url,
                            referer={
                                "sse": "https://www.sse.com.cn/assortment/stock/list/info/announcement/",
                                "szse": "https://www.szse.cn/disclosure/listed/notice/index.html",
                                "cninfo": "https://www.cninfo.com.cn/",
                            }[attempt.provider],
                        )
                        source_name = {
                            "sse": "上海证券交易所正式披露",
                            "szse": "深圳证券交易所正式披露",
                            "cninfo": "巨潮资讯正式披露",
                        }[attempt.provider]
                        document = ingest_downloaded_document(
                            ticker=ticker,
                            content=content,
                            title=attempt.title,
                            source_name=source_name,
                            source_url=attempt.url,
                            published_at=attempt.published_at,
                            provider=attempt.provider,
                            announcement_id=attempt.announcement_id,
                            raw_root=self.raw_root,
                            metadata={
                                **attempt.metadata,
                                "filing_year": attempt.period.year,
                                "filing_kind": attempt.period.kind,
                                "period_end": attempt.period.period_end.isoformat(),
                                "preferred_provider": candidate.provider,
                                "download_fallback": attempt.provider != candidate.provider,
                            },
                        )
                        parsed = (
                            parse_official_document(document)
                            if wants_financials
                            else []
                        )
                        parsed_dimensions = (
                            parse_dimensional_facts(
                                document,
                                data_snapshot_id=sync_result_id,
                            )
                            if wants_dimensions and attempt.period.kind == "annual"
                            else []
                        )
                        if wants_financials and not parsed:
                            document.warnings.append("定期报告已归档，但没有提取到受支持的结构化字段")
                            warnings.append(f"{attempt.title}: 未提取到结构化字段")
                        if (
                            wants_dimensions
                            and attempt.period.kind == "annual"
                            and not parsed_dimensions
                        ):
                            document.warnings.append(
                                "年度报告已归档，但没有提取到受支持的经营维度字段"
                            )
                            warnings.append(f"{attempt.title}: 未提取到经营维度字段")
                        if failures:
                            fallback_count += 1
                            warnings.append(
                                f"{candidate.title}: {candidate.provider}下载失败，已使用{attempt.provider}正式源回退"
                            )
                        documents.append(document)
                        facts.extend(parsed)
                        dimensional_facts.extend(parsed_dimensions)
                        break
                    except Exception as exc:
                        failures.append(f"{attempt.provider}: {exc}")
                else:
                    warnings.append(f"{candidate.title}: 下载或解析失败: {'；'.join(failures)}")

        if wants_financials:
            facts = derive_single_quarter_facts(facts, options.single_quarters)
            facts = _trim_annual_history(facts, options.annual_years)
        sources = list({item.source.source_id: item.source for item in documents}.values())
        provider_results[self.name] = (
            f"正式PDF {len(documents)}/{len(selected)}份，结构化事实{len(facts)}条，"
            f"经营维度事实{len(dimensional_facts)}条，"
            f"正式源回退{fallback_count}份，年度目标{options.annual_years}期，"
            f"单季目标{selected_single_quarters}期"
        )
        result = SyncResult(
            sync_result_id=sync_result_id,
            ticker=ticker,
            scopes=options.scopes,
            company_name=company_name,
            provider_results=provider_results,
            as_of=as_of,
            sources=sources,
            facts=facts,
            dimensional_facts=dimensional_facts,
            documents=documents,
            warnings=warnings,
        )
        return (
            self._attach_announcement_index(result, ticker, options)
            if wants_announcements
            else result
        )

    def _attach_announcement_index(
        self,
        result: SyncResult,
        ticker: str,
        options: SyncRequest,
    ) -> SyncResult:
        as_of = _aware(options.as_of)
        as_of_china = as_of.astimezone(CHINA_TZ)
        start_date = date(
            as_of_china.year - options.announcement_years + 1,
            1,
            1,
        )
        end_date = as_of_china.date()
        candidates: list[AnnouncementCandidate] = []
        timeout = options.request_timeout_seconds

        try:
            cninfo = _CninfoClient(timeout).list_announcements(
                ticker,
                start_date,
                end_date,
                as_of,
                options.max_announcements,
            )
            candidates.extend(cninfo)
            result.provider_results["cninfo_announcements"] = (
                f"元数据成功: {len(cninfo)}条"
            )
        except Exception as exc:
            result.provider_results["cninfo_announcements"] = f"元数据失败: {exc}"
            result.warnings.append(f"巨潮全公告元数据失败: {exc}")

        exchange_name = "sse" if _is_shanghai(ticker) else "szse"
        try:
            if exchange_name == "sse":
                exchange = _SseClient(timeout).list_announcements(
                    ticker,
                    start_date,
                    end_date,
                    as_of,
                    options.max_announcements,
                )
            else:
                exchange = _SzseClient(timeout).list_announcements(
                    ticker,
                    start_date,
                    end_date,
                    as_of,
                    options.max_announcements,
                )
            candidates.extend(exchange)
            result.provider_results[f"{exchange_name}_announcements"] = (
                f"元数据成功: {len(exchange)}条"
            )
        except Exception as exc:
            result.provider_results[f"{exchange_name}_announcements"] = (
                f"元数据失败: {exc}"
            )
            result.warnings.append(f"{exchange_name.upper()}全公告元数据失败: {exc}")

        merged = merge_announcement_candidates(
            candidates,
            max_records=options.max_announcements,
        )
        records = [
            build_announcement_record(
                item,
                data_snapshot_id=result.sync_result_id,
            )
            for item in merged
        ]
        result.announcements.extend(records)
        if records and not result.company_name:
            result.company_name = records[0].company_name
        classified = [
            item
            for item in merged
            if _classification_requested(
                item.classification,
                set(options.scopes),
                set(options.event_types),
            )
        ]
        if not options.download_official_documents or options.max_event_documents == 0:
            result.provider_results["official_announcements"] = (
                f"公告索引{len(records)}条，识别事件候选{len(classified)}条，未下载事件附件"
            )
            return result

        selected = classified[: options.max_event_documents]
        records_by_key = {
            item.canonical_key: item
            for item in result.announcements
        }
        downloaded = 0
        event_count = 0
        fallback_count = 0
        with _http_client(timeout, "https://www.cninfo.com.cn/") as client:
            for merged_item in selected:
                failures = []
                for attempt_index, attempt in enumerate(
                    candidate_download_order(merged_item)
                ):
                    try:
                        content = _download_pdf(
                            client,
                            attempt.url,
                            referer=_provider_referer(attempt.provider),
                        )
                        document = ingest_downloaded_document(
                            ticker=ticker,
                            content=content,
                            title=attempt.title,
                            source_name=_provider_source_name(attempt.provider),
                            source_url=attempt.url,
                            published_at=attempt.published_at,
                            provider=attempt.provider,
                            announcement_id=attempt.announcement_id,
                            raw_root=self.raw_root,
                            metadata={
                                **attempt.metadata,
                                "announcement_category": attempt.category,
                                "canonical_announcement_key": merged_item.canonical_key,
                            },
                        )
                        record = records_by_key[merged_item.canonical_key]
                        attached, event = attach_document_and_event(record, document)
                        records_by_key[merged_item.canonical_key] = attached
                        result.documents.append(document)
                        result.sources.append(document.source)
                        if event is not None:
                            try:
                                event = enrich_event_from_document(event, document)
                            except Exception as exc:
                                warning = f"事件字段解析失败，已保留公告级事件: {exc}"
                                document.warnings.append(warning)
                                result.warnings.append(f"{attempt.title}: {warning}")
                            result.events.append(event)
                            event_count += 1
                        downloaded += 1
                        if attempt_index:
                            fallback_count += 1
                        break
                    except Exception as exc:
                        failures.append(f"{attempt.provider}: {exc}")
                else:
                    result.warnings.append(
                        f"{merged_item.primary.title}: 事件公告下载失败: "
                        f"{'；'.join(failures)}"
                    )

        result.announcements = [
            records_by_key[item.canonical_key]
            for item in merged
            if item.canonical_key in records_by_key
        ]
        result.sources = list(
            {item.source_id: item for item in result.sources}.values()
        )
        result.documents = list(
            {item.document_id: item for item in result.documents}.values()
        )
        result.events = link_event_series(
            list({item.event_id: item for item in result.events}.values())
        )
        truncated = max(0, len(classified) - len(selected))
        result.provider_results["official_announcements"] = (
            f"公告索引{len(records)}条，事件候选{len(classified)}条，"
            f"事件PDF {downloaded}/{len(selected)}份，结构化事件{event_count}条，"
            f"正式源回退{fallback_count}份，因上限未下载{truncated}份"
        )
        return result


class _CninfoClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def list_filings(
        self, ticker: str, start_date: date, end_date: date, as_of: datetime
    ) -> list[FilingCandidate]:
        with _http_client(self.timeout, "https://www.cninfo.com.cn/") as client:
            stock_payload = _get_json(client, CNINFO_STOCK_LIST)
            stock = next((item for item in stock_payload.get("stockList", []) if item.get("code") == ticker), None)
            if stock is None:
                raise RuntimeError(f"巨潮证券列表中未找到{ticker}")
            column = "sse" if _is_shanghai(ticker) else "szse"
            plate = "sh" if _is_shanghai(ticker) else "sz"
            rows: list[dict[str, Any]] = []
            page = 1
            while page <= 10:
                payload = {
                    "pageNum": page,
                    "pageSize": 30,
                    "column": column,
                    "tabName": "fulltext",
                    "plate": plate,
                    "stock": f"{ticker},{stock['orgId']}",
                    "searchkey": "",
                    "secid": "",
                    "category": "category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh",
                    "trade": "",
                    "seDate": f"{start_date.isoformat()}~{end_date.isoformat()}",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "true",
                }
                response = _request(client, "POST", CNINFO_QUERY, data=payload)
                data = response.json()
                page_rows = data.get("announcements") or []
                rows.extend(page_rows)
                total = int(data.get("totalRecordNum") or len(rows))
                if not page_rows or len(rows) >= total:
                    break
                page += 1
        result = []
        for row in rows:
            period = detect_filing_period(str(row.get("announcementTitle") or ""))
            if period is None:
                continue
            published_at = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, tz=timezone.utc)
            if published_at > as_of:
                continue
            result.append(
                FilingCandidate(
                    ticker=ticker,
                    company_name=str(row.get("secName") or stock.get("zwjc") or ticker),
                    title=str(row["announcementTitle"]),
                    published_at=published_at,
                    url=f"https://static.cninfo.com.cn/{str(row['adjunctUrl']).lstrip('/')}",
                    provider="cninfo",
                    announcement_id=str(row.get("announcementId") or ""),
                    period=period,
                    metadata={"org_id": row.get("orgId"), "cninfo_column": row.get("pageColumn")},
                )
            )
        return result

    def list_announcements(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        as_of: datetime,
        max_records: int,
    ) -> list[AnnouncementCandidate]:
        with _http_client(self.timeout, "https://www.cninfo.com.cn/") as client:
            stock_payload = _get_json(client, CNINFO_STOCK_LIST)
            stock = next(
                (
                    item
                    for item in stock_payload.get("stockList", [])
                    if item.get("code") == ticker
                ),
                None,
            )
            if stock is None:
                raise RuntimeError(f"巨潮证券列表中未找到{ticker}")
            column = "sse" if _is_shanghai(ticker) else "szse"
            plate = "sh" if _is_shanghai(ticker) else "sz"
            rows: list[dict[str, Any]] = []
            page = 1
            page_size = 30
            page_limit = max(1, math.ceil(max_records / page_size))
            while page <= page_limit:
                payload = {
                    "pageNum": page,
                    "pageSize": page_size,
                    "column": column,
                    "tabName": "fulltext",
                    "plate": plate,
                    "stock": f"{ticker},{stock['orgId']}",
                    "searchkey": "",
                    "secid": "",
                    "category": "",
                    "trade": "",
                    "seDate": f"{start_date.isoformat()}~{end_date.isoformat()}",
                    "sortName": "announcementTime",
                    "sortType": "desc",
                    "isHLtitle": "true",
                }
                response = _request(client, "POST", CNINFO_QUERY, data=payload)
                data = response.json()
                page_rows = data.get("announcements") or []
                rows.extend(page_rows)
                total = int(data.get("totalRecordNum") or len(rows))
                if not page_rows or len(rows) >= total or len(rows) >= max_records:
                    break
                page += 1
        result = []
        for row in rows[:max_records]:
            timestamp = row.get("announcementTime")
            adjunct_url = str(row.get("adjunctUrl") or "")
            if timestamp is None or not adjunct_url:
                continue
            published_at = datetime.fromtimestamp(
                int(timestamp) / 1000,
                tz=timezone.utc,
            )
            if published_at > as_of:
                continue
            result.append(
                AnnouncementCandidate(
                    ticker=ticker,
                    company_name=str(
                        row.get("secName") or stock.get("zwjc") or ticker
                    ),
                    title=str(row.get("announcementTitle") or ""),
                    published_at=published_at,
                    url=(
                        "https://static.cninfo.com.cn/"
                        + adjunct_url.lstrip("/")
                    ),
                    provider="cninfo",
                    announcement_id=str(row.get("announcementId") or ""),
                    category=(
                        str(
                            row.get("categoryName")
                            or row.get("announcementTypeName")
                            or row.get("pageColumn")
                            or ""
                        )
                        or None
                    ),
                    metadata={
                        "org_id": row.get("orgId"),
                        "cninfo_column": row.get("pageColumn"),
                        "adjunct_type": row.get("adjunctType"),
                        "adjunct_size": row.get("adjunctSize"),
                    },
                )
            )
        return result


class _SseClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def list_filings(
        self, ticker: str, start_date: date, end_date: date, as_of: datetime
    ) -> list[FilingCandidate]:
        if not _is_shanghai(ticker):
            return []
        rows: list[dict[str, Any]] = []
        with _http_client(self.timeout, "https://www.sse.com.cn/assortment/stock/list/info/announcement/") as client:
            for page in range(1, 8):
                params = {
                    "isPagination": "true",
                    "productId": ticker,
                    "keyWord": "",
                    "securityType": "0101,120100,020100,020200,120200",
                    "reportType2": "DQBG",
                    "reportType": "ALL",
                    "beginDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "pageHelp.pageSize": "100",
                    "pageHelp.pageCount": "50",
                    "pageHelp.pageNo": str(page),
                    "pageHelp.beginPage": str(page),
                    "pageHelp.cacheSize": "1",
                    "pageHelp.endPage": str(page + 4),
                    "_": str(int(time.time() * 1000)),
                }
                data = _get_json(client, SSE_QUERY, params=params)
                page_help = data.get("pageHelp") or {}
                page_rows = page_help.get("data") or []
                rows.extend(page_rows)
                page_count = int(page_help.get("pageCount") or 1)
                if not page_rows or page >= page_count:
                    break
        result = []
        for row in rows:
            title = str(row.get("TITLE") or "")
            period = detect_filing_period(title)
            if period is None:
                continue
            published_at = _china_midnight(str(row.get("SSEDATE") or row.get("ADDDATE") or "")[:10])
            if published_at > as_of:
                continue
            path = str(row.get("URL") or "")
            result.append(
                FilingCandidate(
                    ticker=ticker,
                    company_name=str(row.get("SECURITY_NAME") or ticker),
                    title=title,
                    published_at=published_at,
                    url=f"https://www.sse.com.cn/{path.lstrip('/')}",
                    provider="sse",
                    announcement_id=path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                    period=period,
                    metadata={"bulletin_type": row.get("BULLETIN_TYPE")},
                )
            )
        return result

    def list_announcements(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        as_of: datetime,
        max_records: int,
    ) -> list[AnnouncementCandidate]:
        if not _is_shanghai(ticker):
            return []
        rows: list[dict[str, Any]] = []
        page_size = 100
        page_limit = max(1, math.ceil(max_records / page_size))
        with _http_client(
            self.timeout,
            "https://www.sse.com.cn/assortment/stock/list/info/announcement/",
        ) as client:
            for page in range(1, page_limit + 1):
                params = {
                    "isPagination": "true",
                    "productId": ticker,
                    "keyWord": "",
                    "securityType": "0101,120100,020100,020200,120200",
                    "reportType2": "",
                    "reportType": "ALL",
                    "beginDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "pageHelp.pageSize": str(page_size),
                    "pageHelp.pageCount": str(page_limit),
                    "pageHelp.pageNo": str(page),
                    "pageHelp.beginPage": str(page),
                    "pageHelp.cacheSize": "1",
                    "pageHelp.endPage": str(min(page + 4, page_limit)),
                    "_": str(int(time.time() * 1000)),
                }
                data = _get_json(client, SSE_QUERY, params=params)
                page_help = data.get("pageHelp") or {}
                page_rows = page_help.get("data") or []
                rows.extend(page_rows)
                page_count = int(page_help.get("pageCount") or 1)
                if (
                    not page_rows
                    or page >= page_count
                    or len(rows) >= max_records
                ):
                    break
        result = []
        for row in rows[:max_records]:
            date_text = str(row.get("SSEDATE") or row.get("ADDDATE") or "")[:10]
            path = str(row.get("URL") or "")
            if not date_text or not path:
                continue
            published_at = _china_midnight(date_text)
            if published_at > as_of:
                continue
            result.append(
                AnnouncementCandidate(
                    ticker=ticker,
                    company_name=str(row.get("SECURITY_NAME") or ticker),
                    title=str(row.get("TITLE") or ""),
                    published_at=published_at,
                    url=(
                        path
                        if path.startswith(("http://", "https://"))
                        else f"https://www.sse.com.cn/{path.lstrip('/')}"
                    ),
                    provider="sse",
                    announcement_id=(
                        path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                    ),
                    category=(
                        str(
                            row.get("BULLETIN_TYPE")
                            or row.get("BULLETIN_TYPE_CODE")
                            or ""
                        )
                        or None
                    ),
                    metadata={
                        "bulletin_type": row.get("BULLETIN_TYPE"),
                        "bulletin_type_code": row.get("BULLETIN_TYPE_CODE"),
                    },
                )
            )
        return result


class _SzseClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def list_filings(
        self, ticker: str, start_date: date, end_date: date, as_of: datetime
    ) -> list[FilingCandidate]:
        if _is_shanghai(ticker):
            return []
        rows: list[dict[str, Any]] = []
        with _http_client(self.timeout, "https://www.szse.cn/disclosure/listed/notice/index.html") as client:
            for page in range(1, 12):
                body = {
                    "seDate": [start_date.isoformat(), end_date.isoformat()],
                    "stock": [ticker],
                    "channelCode": ["listedNotice_disc"],
                    "pageSize": 50,
                    "pageNum": page,
                }
                response = _request(
                    client,
                    "POST",
                    SZSE_QUERY,
                    params={"random": str(time.time())},
                    json=body,
                )
                data = response.json()
                page_rows = data.get("data") or []
                rows.extend(page_rows)
                total = int(data.get("announceCount") or len(rows))
                if not page_rows or len(rows) >= total:
                    break
        result = []
        for row in rows:
            title = str(row.get("title") or "")
            period = detect_filing_period(title)
            if period is None:
                continue
            published_at = _china_midnight(str(row.get("publishTime") or "")[:10])
            if published_at > as_of:
                continue
            attach_path = str(row.get("attachPath") or "")
            codes = row.get("secCode") or []
            names = row.get("secName") or []
            result.append(
                FilingCandidate(
                    ticker=ticker,
                    company_name=str(names[0] if names else ticker),
                    title=title,
                    published_at=published_at,
                    url=f"https://disc.static.szse.cn/download/{attach_path.lstrip('/')}",
                    provider="szse",
                    announcement_id=str(row.get("annId") or row.get("id") or ""),
                    period=period,
                    metadata={"security_codes": codes, "attachment_size_kb": row.get("attachSize")},
                )
            )
        return result

    def list_announcements(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        as_of: datetime,
        max_records: int,
    ) -> list[AnnouncementCandidate]:
        if _is_shanghai(ticker):
            return []
        rows: list[dict[str, Any]] = []
        page_size = 50
        page_limit = max(1, math.ceil(max_records / page_size))
        with _http_client(
            self.timeout,
            "https://www.szse.cn/disclosure/listed/notice/index.html",
        ) as client:
            for page in range(1, page_limit + 1):
                body = {
                    "seDate": [start_date.isoformat(), end_date.isoformat()],
                    "stock": [ticker],
                    "channelCode": ["listedNotice_disc"],
                    "pageSize": page_size,
                    "pageNum": page,
                }
                response = _request(
                    client,
                    "POST",
                    SZSE_QUERY,
                    params={"random": str(time.time())},
                    json=body,
                )
                data = response.json()
                page_rows = data.get("data") or []
                rows.extend(page_rows)
                total = int(data.get("announceCount") or len(rows))
                if (
                    not page_rows
                    or len(rows) >= total
                    or len(rows) >= max_records
                ):
                    break
        result = []
        for row in rows[:max_records]:
            publish_text = str(row.get("publishTime") or "")[:10]
            attach_path = str(row.get("attachPath") or "")
            if not publish_text or not attach_path:
                continue
            published_at = _china_midnight(publish_text)
            if published_at > as_of:
                continue
            codes = _as_list(row.get("secCode"))
            names = _as_list(row.get("secName"))
            result.append(
                AnnouncementCandidate(
                    ticker=ticker,
                    company_name=str(names[0] if names else ticker),
                    title=str(row.get("title") or ""),
                    published_at=published_at,
                    url=(
                        attach_path
                        if attach_path.startswith(("http://", "https://"))
                        else (
                            "https://disc.static.szse.cn/download/"
                            + attach_path.lstrip("/")
                        )
                    ),
                    provider="szse",
                    announcement_id=str(
                        row.get("annId") or row.get("id") or ""
                    ),
                    category=(
                        str(
                            row.get("categoryName")
                            or row.get("bigCategoryName")
                            or row.get("channelName")
                            or ""
                        )
                        or None
                    ),
                    metadata={
                        "security_codes": codes,
                        "attachment_size_kb": row.get("attachSize"),
                    },
                )
            )
        return result


def _select_filings(
    candidates: list[FilingCandidate], annual_years: int, single_quarters: int
) -> list[FilingCandidate]:
    provider_priority = {"cninfo": 0, "sse": 1, "szse": 1}
    selected_by_key: dict[tuple[int, str], FilingCandidate] = {}
    for candidate in candidates:
        previous = selected_by_key.get(candidate.key)
        rank = (candidate.published_at, provider_priority.get(candidate.provider, 0))
        previous_rank = (
            (previous.published_at, provider_priority.get(previous.provider, 0)) if previous else None
        )
        if previous is None or rank > previous_rank:
            selected_by_key[candidate.key] = candidate
    annuals = sorted(
        (item for item in selected_by_key.values() if item.period.kind == "annual"),
        key=lambda item: item.period.period_end,
        reverse=True,
    )[:annual_years]
    if single_quarters <= 0:
        return sorted(
            annuals,
            key=lambda item: (item.period.period_end, item.published_at),
        )
    if selected_by_key:
        latest_year = max(item.period.year for item in selected_by_key.values())
    else:
        latest_year = date.today().year
    interim_years = math.ceil(single_quarters / 4) + 1
    interim = [
        item
        for item in selected_by_key.values()
        if item.period.kind in {"q1", "h1", "q3"} and item.period.year >= latest_year - interim_years + 1
    ]
    annual_for_quarters = [
        item
        for item in selected_by_key.values()
        if item.period.kind == "annual" and item.period.year >= latest_year - interim_years
    ]
    unique = {item.key: item for item in [*annuals, *interim, *annual_for_quarters]}
    return sorted(unique.values(), key=lambda item: (item.period.period_end, item.published_at))


def _candidate_attempts(
    primary: FilingCandidate, candidates: list[FilingCandidate]
) -> list[FilingCandidate]:
    alternatives = [
        item
        for item in candidates
        if item.key == primary.key
        and item.url != primary.url
        and item.published_at <= primary.published_at
    ]
    alternatives.sort(
        key=lambda item: (
            item.title == primary.title,
            item.provider == "cninfo",
            item.published_at,
        ),
        reverse=True,
    )
    return [primary, *alternatives]


def _trim_annual_history(facts: list[FactRecord], annual_years: int) -> list[FactRecord]:
    annual_by_metric: dict[str, set[date]] = {}
    for item in facts:
        if item.period_type == "annual" and item.period_end:
            annual_by_metric.setdefault(item.metric_id, set()).add(item.period_end)
    keep_periods = {
        metric_id: set(sorted(periods, reverse=True)[:annual_years])
        for metric_id, periods in annual_by_metric.items()
    }
    return [
        item
        for item in facts
        if item.period_type != "annual"
        or (item.period_end is not None and item.period_end in keep_periods.get(item.metric_id, set()))
    ]


def _http_client(timeout: float, referer: str) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
    )


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url}: {last_error}")


def _get_json(client: httpx.Client, url: str, **kwargs) -> dict[str, Any]:
    response = _request(client, "GET", url, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"接口未返回JSON: {url}") from exc


def _download_pdf(client: httpx.Client, url: str, *, referer: str | None = None) -> bytes:
    response = _request(
        client,
        "GET",
        url,
        headers={
            "Accept": "application/pdf,*/*",
            "User-Agent": client.headers["User-Agent"],
            "Referer": referer or client.headers.get("Referer", "https://www.cninfo.com.cn/"),
        },
    )
    if not response.content.startswith(b"%PDF"):
        raise RuntimeError(f"下载内容不是PDF，content-type={response.headers.get('content-type')}")
    return response.content


def _classification_requested(
    classification: EventClassification,
    scopes: set[str],
    event_types: set[str] | None = None,
) -> bool:
    if classification.event_type == "other":
        return False
    if event_types and classification.event_type not in event_types:
        return False
    if "announcements" in scopes:
        return True
    requested_steps = set()
    if "governance" in scopes:
        requested_steps.add(3)
    if "capital_actions" in scopes:
        requested_steps.add(4)
    if "risks" in scopes:
        requested_steps.add(6)
    return bool(requested_steps & set(classification.report_steps))


def _provider_referer(provider: str) -> str:
    return {
        "sse": "https://www.sse.com.cn/assortment/stock/list/info/announcement/",
        "szse": "https://www.szse.cn/disclosure/listed/notice/index.html",
        "cninfo": "https://www.cninfo.com.cn/",
    }.get(provider, "https://www.cninfo.com.cn/")


def _provider_source_name(provider: str) -> str:
    return {
        "sse": "上海证券交易所正式披露",
        "szse": "深圳证券交易所正式披露",
        "cninfo": "巨潮资讯正式披露",
    }.get(provider, f"{provider}正式披露")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _normalize_ticker(ticker: str) -> str:
    digits = "".join(char for char in ticker if char.isdigit())
    if len(digits) != 6:
        raise ValueError("A股代码必须包含6位数字")
    return digits


def _is_shanghai(ticker: str) -> bool:
    return ticker.startswith(("5", "6", "9"))


def _china_midnight(value: str) -> datetime:
    parsed = date.fromisoformat(value)
    return datetime.combine(parsed, datetime_time.min, tzinfo=CHINA_TZ).astimezone(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
