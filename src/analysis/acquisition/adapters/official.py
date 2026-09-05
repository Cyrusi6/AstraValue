from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .base import (
    BoundedTransportEnvelope,
    CompanyBootstrapResult,
    DiscoveryResult,
    FetchWork,
    NormalizedResource,
    QueryWork,
    SnapshotReader,
    TransportClient,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")


class OfficialAcquisitionAdapter:
    adapter_key = "official"

    def __init__(
        self,
        transport: TransportClient,
        snapshot_reader: SnapshotReader,
    ) -> None:
        self._transport = transport
        self._snapshot_reader = snapshot_reader

    def bootstrap_company(
        self,
        ticker: str,
        query: QueryWork | None = None,
    ) -> CompanyBootstrapResult:
        exchange = "sse" if ticker.startswith(("5", "6", "9")) else "szse"
        # A network bootstrap is represented as an ordinary registered query;
        # callers persist its attempt before invoking execute_query.  This pure
        # fallback contains no guessed listing/prospectus date.
        return CompanyBootstrapResult(
            ticker=ticker,
            company_name=None,
            exchange=exchange,
            anchor_evidence=(() if query is None else (query.query_id,)),
        )

    def execute_query(self, work: QueryWork) -> BoundedTransportEnvelope:
        return self._transport.request(work)

    def fetch_resource(self, work: FetchWork) -> BoundedTransportEnvelope:
        headers = dict(work.query.headers)
        if etag := work.validators.get("etag"):
            headers["If-None-Match"] = etag
        if modified := work.validators.get("last_modified"):
            headers["If-Modified-Since"] = modified
        fetch_query = QueryWork(
            source_definition_id=work.query.source_definition_id,
            source_definition_version=work.query.source_definition_version,
            query_id=work.query.query_id,
            query_family=work.query.query_family,
            execution_key=work.query.execution_key,
            method="GET",
            url=work.resource.url,
            page=work.query.page,
            cursor=work.query.cursor,
            headers=headers,
            expected_mime_types=("application/pdf", "text/html"),
            max_response_bytes=work.query.max_response_bytes,
            parser_schema_version=work.query.parser_schema_version,
            context=work.query.context,
            execution_capability=work.query.execution_capability,
        )
        return self._transport.request(fetch_query)

    def parse_retained_discovery(
        self,
        snapshot_id: str,
        work: QueryWork,
    ) -> DiscoveryResult:
        body = self._snapshot_reader(snapshot_id)
        return self._parse(body, work, replayable=True)

    def validate_and_normalize_without_retention(
        self,
        envelope: BoundedTransportEnvelope,
        work: QueryWork,
    ) -> DiscoveryResult:
        if len(envelope.body) > work.max_response_bytes:
            raise ValueError("bounded discovery envelope exceeds query policy")
        return self._parse(envelope.body, work, replayable=False)

    def _parse(self, body: bytes, work: QueryWork, *, replayable: bool) -> DiscoveryResult:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("discovery response is not valid UTF-8 JSON") from exc
        rows, declared_total, page_count, terminal, next_cursor = self._rows_and_page(
            payload, work
        )
        resources = tuple(
            self._normalize_row(row, index, work)
            for index, row in enumerate(rows)
        )
        return DiscoveryResult(
            resources=resources,
            schema_valid=True,
            parser_schema_version=work.parser_schema_version,
            declared_total=declared_total,
            normalized_total=len(resources),
            page=work.page,
            page_count=page_count,
            next_cursor=next_cursor,
            terminal=terminal,
            response_sha256=hashlib.sha256(body).hexdigest(),
            response_length=len(body),
            replayable=replayable,
        )

    def _rows_and_page(
        self,
        payload: Any,
        work: QueryWork,
    ) -> tuple[list[Mapping[str, Any]], int, int | None, bool, str | None]:
        raise NotImplementedError

    def _normalize_row(
        self,
        row: Mapping[str, Any],
        index: int,
        work: QueryWork,
    ) -> NormalizedResource:
        raise NotImplementedError


class CninfoAcquisitionAdapter(OfficialAcquisitionAdapter):
    adapter_key = "cninfo"

    def _rows_and_page(self, payload: Any, work: QueryWork):
        if work.query_family == "company_bootstrap":
            if not isinstance(payload, dict) or not isinstance(
                payload.get("stockList"), list
            ):
                raise ValueError("cninfo stockList schema mismatch")
            ticker = str(work.context.get("ticker") or "")
            rows = [
                item
                for item in payload["stockList"]
                if isinstance(item, dict) and str(item.get("code") or "") == ticker
            ]
            return rows, len(rows), 1, True, None
        if not isinstance(payload, dict) or not isinstance(payload.get("announcements"), list):
            if (
                isinstance(payload, dict)
                and "announcements" in payload
                and payload["announcements"] is None
                and work.parser_schema_version in {"2", "3"}
                and work.context.get("schema_id") == "cninfo.announcements"
            ):
                return self._nullable_empty_page(payload, work)
            raise ValueError("cninfo announcements schema mismatch")
        rows = payload["announcements"]
        if not all(isinstance(item, dict) for item in rows):
            raise ValueError("cninfo announcement row schema mismatch")
        total_path = str(work.context.get("total_path") or "totalRecordNum")
        if total_path not in payload:
            raise ValueError(
                f"cninfo {total_path} is required for termination proof"
            )
        total = int(payload[total_path])
        page_size = int(work.context.get("page_size") or max(1, len(rows)))
        page_count = (total + page_size - 1) // page_size if total else 1
        terminal = not rows or work.page >= page_count
        return rows, total, page_count, terminal, None if terminal else str(work.page + 1)

    @staticmethod
    def _nullable_empty_page(payload: dict[str, Any], work: QueryWork):
        allowed = {
            "announcements", "totalAnnouncement", "hasMore",
            "totalRecordNum", "totalSecurities", "totalpages",
            "classifiedAnnouncements", "categoryList",
        }
        if (
            set(payload) - allowed
            or work.page != 1
            or work.cursor is not None
            or work.context.get("total_path") != "totalAnnouncement"
            or type(payload.get("totalAnnouncement")) is not int
            or payload["totalAnnouncement"] != 0
            or payload.get("hasMore") is not False
        ):
            raise ValueError("cninfo nullable empty response contract mismatch")
        for name in ("totalRecordNum", "totalSecurities", "totalpages"):
            if name in payload and (type(payload[name]) is not int or payload[name] != 0):
                raise ValueError("cninfo nullable empty response count mismatch")
        for name in ("classifiedAnnouncements", "categoryList"):
            if name in payload and payload[name] is not None:
                raise ValueError("cninfo nullable empty response contains unsupported groups")
        return [], 0, 1, True, None

    def _normalize_row(self, row, index, work):
        if work.query_family == "company_bootstrap":
            ticker = str(row.get("code") or "").strip()
            org_id = str(row.get("orgId") or "").strip()
            title = str(row.get("zwjc") or ticker).strip()
            if not ticker or not org_id:
                raise ValueError("cninfo stock row misses code/orgId")
            return _resource(
                canonical_id=f"cninfo-org:{ticker}:{org_id}",
                upstream_id=f"cninfo-company:{ticker}",
                title=title,
                url=work.url,
                raw=None,
                published=None,
                precision="unknown",
                timezone_name="Asia/Shanghai",
                row=row,
                row_locator=f"page:{work.page}/stockList:{index}",
                required_fetch=False,
                metadata={
                    "ticker": ticker,
                    "org_id": org_id,
                    "wire_stock": f"{ticker},{org_id}",
                    "company_name": title,
                },
            )
        announcement_id = str(row.get("announcementId") or "").strip()
        adjunct = str(row.get("adjunctUrl") or "").strip()
        title = str(row.get("announcementTitle") or "").strip()
        timestamp = row.get("announcementTime")
        if not announcement_id or not adjunct or not title or timestamp is None:
            raise ValueError("cninfo row misses canonical fields")
        published = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
        url = adjunct if adjunct.startswith("https://") else urljoin(
            "https://static.cninfo.com.cn/", adjunct.lstrip("/")
        )
        expected_mime = ("application/pdf",)
        if (work.parser_schema_version == "3"
                and work.context.get("schema_id") == "cninfo.announcements"):
            suffix = urlsplit(url).path.lower().rsplit(".", 1)[-1]
            if suffix in {"html", "htm"}:
                expected_mime = ("text/html",)
            elif suffix != "pdf":
                raise ValueError("cninfo unsupported announcement attachment format")
        return _resource(
            canonical_id=f"cninfo:{announcement_id}",
            upstream_id=f"disclosure:{announcement_id}",
            title=title,
            url=url,
            raw=str(timestamp),
            published=published,
            precision="instant",
            timezone_name="Asia/Shanghai",
            row=row,
            row_locator=f"page:{work.page}/announcements:{index}",
            required_fetch=str(work.context.get("fetch_policy", "required_attachment"))
            == "required_attachment",
            metadata={"expected_mime_types": expected_mime},
        )


class SseAcquisitionAdapter(OfficialAcquisitionAdapter):
    adapter_key = "sse"

    def _rows_and_page(self, payload: Any, work: QueryWork):
        if not isinstance(payload, dict) or not isinstance(payload.get("pageHelp"), dict):
            raise ValueError("sse pageHelp schema mismatch")
        page_help = payload["pageHelp"]
        rows = page_help.get("data")
        if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
            raise ValueError("sse data schema mismatch")
        if "pageCount" not in page_help or "total" not in page_help and "totalCount" not in page_help:
            raise ValueError("sse count fields are required for termination proof")
        page_count = int(page_help["pageCount"])
        total = int(page_help.get("total") or page_help.get("totalCount") or 0)
        terminal = not rows or work.page >= page_count
        return rows, total, page_count, terminal, None if terminal else str(work.page + 1)

    def _normalize_row(self, row, index, work):
        path = str(row.get("URL") or "").strip()
        # The currently observed company-statement response uses lowercase
        # ``title``.  Keep the historical uppercase spelling readable because
        # frozen v1.0-v1.2 discovery snapshots must remain replayable.
        title = str(row.get("title") or row.get("TITLE") or "").strip()
        raw_date = str(row.get("SSEDATE") or row.get("ADDDATE") or "")[:10]
        if not path or not title or not raw_date:
            raise ValueError("sse row misses canonical fields")
        published = _conservative_date_boundary(raw_date)
        announcement_id = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return _resource(
            canonical_id=f"sse:{announcement_id}",
            upstream_id=f"disclosure:{announcement_id}",
            title=title,
            url=path if path.startswith("https://") else urljoin("https://www.sse.com.cn/", path.lstrip("/")),
            raw=raw_date,
            published=published,
            precision="date",
            timezone_name="Asia/Shanghai",
            row=row,
            row_locator=f"page:{work.page}/pageHelp.data:{index}",
            required_fetch=str(work.context.get("fetch_policy", "required_attachment"))
            == "required_attachment",
            metadata={"expected_mime_types": ("application/pdf",)},
        )


class SzseAcquisitionAdapter(OfficialAcquisitionAdapter):
    adapter_key = "szse"

    def _rows_and_page(self, payload: Any, work: QueryWork):
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("szse data schema mismatch")
        rows = payload["data"]
        if not all(isinstance(item, dict) for item in rows):
            raise ValueError("szse row schema mismatch")
        if "announceCount" not in payload:
            raise ValueError("szse announceCount is required for termination proof")
        total = int(payload["announceCount"])
        page_size = int(work.context.get("page_size") or max(1, len(rows)))
        page_count = (total + page_size - 1) // page_size if total else 1
        terminal = not rows or work.page >= page_count
        return rows, total, page_count, terminal, None if terminal else str(work.page + 1)

    def _normalize_row(self, row, index, work):
        path = str(row.get("attachPath") or "").strip()
        title = str(row.get("title") or "").strip()
        raw_date = str(row.get("publishTime") or "").strip()
        announcement_id = str(row.get("annId") or row.get("id") or "").strip()
        if not path or not title or not raw_date or not announcement_id:
            raise ValueError("szse row misses canonical fields")
        published, precision = _parse_china_published(raw_date)
        return _resource(
            canonical_id=f"szse:{announcement_id}",
            upstream_id=f"disclosure:{announcement_id}",
            title=title,
            url=path if path.startswith("https://") else urljoin("https://disc.static.szse.cn/download/", path.lstrip("/")),
            raw=raw_date,
            published=published,
            precision=precision,
            timezone_name="Asia/Shanghai",
            row=row,
            row_locator=f"page:{work.page}/data:{index}",
            required_fetch=str(work.context.get("fetch_policy", "required_attachment"))
            == "required_attachment",
            metadata={"expected_mime_types": ("application/pdf",)},
        )


class MoutaiIrAcquisitionAdapter(OfficialAcquisitionAdapter):
    adapter_key = "moutai_ir"

    def _rows_and_page(self, payload: Any, work: QueryWork):
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("moutai IR items schema mismatch")
        rows = payload["items"]
        if not all(isinstance(item, dict) for item in rows):
            raise ValueError("moutai IR item schema mismatch")
        if "total" not in payload or "has_more" not in payload:
            raise ValueError("moutai IR termination fields are required")
        total = int(payload["total"])
        terminal = not bool(payload["has_more"])
        next_cursor = None if terminal else str(payload.get("next_cursor") or "")
        if not terminal and not next_cursor:
            raise ValueError("moutai IR next cursor missing")
        return rows, total, None, terminal, next_cursor

    def _normalize_row(self, row, index, work):
        identifier = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        raw_date = str(row.get("published_at") or "").strip()
        if not identifier or not title or not url:
            raise ValueError("moutai IR item misses canonical fields")
        published: datetime | None = None
        precision = "unknown"
        if raw_date:
            try:
                published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = _conservative_date_boundary(raw_date[:10])
                    precision = "date"
                else:
                    published = published.astimezone(timezone.utc)
                    precision = "instant"
            except ValueError:
                published = _conservative_date_boundary(raw_date[:10])
                precision = "date"
        return _resource(
            canonical_id=f"moutai-ir:{identifier}",
            upstream_id=f"moutai-publication:{identifier}",
            title=title,
            url=url,
            raw=raw_date or None,
            published=published,
            precision=precision,
            timezone_name="Asia/Shanghai",
            row=row,
            row_locator=f"page:{work.page}/items:{index}",
            required_fetch=str(work.context.get("fetch_policy", "required_attachment"))
            == "required_attachment",
            metadata={"expected_mime_types": ("application/pdf", "text/html")},
        )


def _resource(
    *,
    canonical_id: str,
    upstream_id: str,
    title: str,
    url: str,
    raw: str | None,
    published: datetime | None,
    precision: str,
    timezone_name: str,
    row: Mapping[str, Any],
    row_locator: str,
    required_fetch: bool,
    metadata: Mapping[str, Any] | None = None,
) -> NormalizedResource:
    encoded = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return NormalizedResource(
        canonical_resource_id=canonical_id,
        upstream_material_id=upstream_id,
        title=title,
        url=url,
        published_raw=raw,
        published_at=published,
        published_at_precision=precision,
        source_timezone=timezone_name,
        row_locator=row_locator,
        row_hash=hashlib.sha256(encoded).hexdigest(),
        required_fetch=required_fetch,
        metadata=dict(metadata or {}),
    )


def _conservative_date_boundary(value: str) -> datetime:
    parsed = date.fromisoformat(value)
    next_day = parsed.fromordinal(parsed.toordinal() + 1)
    return datetime.combine(next_day, time.min, tzinfo=CHINA_TZ).astimezone(timezone.utc)


def _parse_china_published(value: str) -> tuple[datetime, str]:
    normalized = value.strip().replace("/", "-")
    if len(normalized) <= 10:
        return _conservative_date_boundary(normalized[:10]), "date"
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return _conservative_date_boundary(normalized[:10]), "date"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(timezone.utc), "instant"
