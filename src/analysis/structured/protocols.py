from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence


class ProtocolFamily(str, Enum):
    EM_S = "EM-S"
    EM_W = "EM-W"
    EM_M = "EM-M"
    EM_F = "EM-F"
    EM_Q = "EM-Q"
    BAOSTOCK = "BS"


class ResultStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    FAILED = "failed"
    PARTIAL = "partial"


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionProof:
    evidence_kind: str
    status: ResultStatus
    response_sha256: str
    rows_yielded: int
    http_status: int | None = None
    sdk_code: str | None = None
    result_set_exhausted: bool | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.evidence_kind not in {"http_response", "sdk_result"}:
            raise ValueError("unsupported execution evidence kind")
        if self.rows_yielded < 0:
            raise ValueError("rows_yielded cannot be negative")
        digest = self.response_sha256.lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("execution proof requires a full SHA-256")
        object.__setattr__(self, "response_sha256", digest)
        if self.evidence_kind == "http_response":
            if self.http_status is None or not 100 <= self.http_status <= 599:
                raise ValueError("HTTP proof requires an actual HTTP status")
            if self.sdk_code is not None or self.result_set_exhausted is not None:
                raise ValueError("HTTP proof cannot carry SDK success evidence")
        else:
            if self.http_status is not None:
                raise ValueError("SDK proof cannot forge an HTTP status")
            if self.sdk_code is None or self.result_set_exhausted is None:
                raise ValueError("SDK proof requires result code and exhaustion state")
            if self.status in {ResultStatus.SUCCESS, ResultStatus.EMPTY}:
                if self.sdk_code != "0" or not self.result_set_exhausted:
                    raise ValueError("successful SDK proof requires code 0 and exhaustion")
            if self.status is ResultStatus.EMPTY and self.rows_yielded:
                raise ValueError("empty SDK proof cannot contain rows")


@dataclass(frozen=True, slots=True)
class ProtocolPage:
    protocol: ProtocolFamily
    status: ResultStatus
    page_number: int
    rows: tuple[Mapping[str, Any], ...]
    declared_total: int | None
    declared_pages: int | None
    terminal: bool
    proof: ExecutionProof
    business_code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number must be positive")
        if self.declared_total is not None and self.declared_total < 0:
            raise ValueError("declared_total cannot be negative")
        if self.declared_pages is not None and self.declared_pages < 0:
            raise ValueError("declared_pages cannot be negative")
        if self.status is ResultStatus.EMPTY and self.rows:
            raise ValueError("empty protocol result cannot contain rows")
        if self.status is ResultStatus.FAILED and self.terminal:
            raise ValueError("failed protocol result cannot prove a terminal page")


EASTMONEY_ENDPOINTS: Mapping[ProtocolFamily, frozenset[str]] = {
    ProtocolFamily.EM_S: frozenset(
        {"https://datacenter.eastmoney.com/securities/api/data/v1/get"}
    ),
    ProtocolFamily.EM_W: frozenset(
        {"https://datacenter-web.eastmoney.com/api/data/v1/get"}
    ),
    ProtocolFamily.EM_M: frozenset(
        {"https://datacenter.eastmoney.com/securities/api/data/get"}
    ),
    ProtocolFamily.EM_F: frozenset(
        {
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index",
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/zcfzbDateAjaxNew",
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/zcfzbAjaxNew",
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/lrbDateAjaxNew",
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/lrbAjaxNew",
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/xjllbDateAjaxNew",
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/xjllbAjaxNew",
        }
    ),
    ProtocolFamily.EM_Q: frozenset(
        {"https://push2.eastmoney.com/api/qt/stock/get"}
    ),
}


_ALLOWED_PARAMS: Mapping[ProtocolFamily, frozenset[str]] = {
    ProtocolFamily.EM_S: frozenset(
        {
            "reportName",
            "columns",
            "filter",
            "pageNumber",
            "pageSize",
            "sortColumns",
            "sortTypes",
            "source",
            "client",
            "quoteColumns",
        }
    ),
    ProtocolFamily.EM_W: frozenset(
        {
            "reportName",
            "columns",
            "filter",
            "pageNumber",
            "pageSize",
            "sortColumns",
            "sortTypes",
            "source",
            "client",
            "quoteColumns",
        }
    ),
    ProtocolFamily.EM_M: frozenset(
        {"type", "sty", "filter", "p", "ps", "sr", "st", "source", "client", "v"}
    ),
    ProtocolFamily.EM_F: frozenset(
        {"type", "companyType", "reportDateType", "reportType", "dates", "code"}
    ),
    ProtocolFamily.EM_Q: frozenset({"secid", "fields", "fltt", "invt", "ut"}),
}


_REQUIRED_PARAMS: Mapping[ProtocolFamily, frozenset[str]] = {
    ProtocolFamily.EM_S: frozenset(
        {"reportName", "columns", "filter", "pageNumber", "pageSize", "sortColumns", "sortTypes"}
    ),
    ProtocolFamily.EM_W: frozenset(
        {"reportName", "columns", "filter", "pageNumber", "pageSize", "sortColumns", "sortTypes"}
    ),
    ProtocolFamily.EM_M: frozenset({"type", "sty", "filter", "p", "ps", "sr", "st"}),
    ProtocolFamily.EM_F: frozenset(),
    ProtocolFamily.EM_Q: frozenset({"secid", "fields"}),
}


@dataclass(frozen=True, slots=True)
class EastmoneyRequest:
    protocol: ProtocolFamily
    endpoint: str
    params: Mapping[str, str]
    max_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.protocol not in EASTMONEY_ENDPOINTS:
            raise ValueError("request is not an Eastmoney HTTP protocol")
        if self.endpoint not in EASTMONEY_ENDPOINTS[self.protocol]:
            raise ValueError("endpoint is outside the fixed protocol allowlist")
        if self.max_response_bytes <= 0:
            raise ValueError("response bound must be positive")
        params = {str(key): str(value) for key, value in self.params.items()}
        unknown = sorted(set(params).difference(_ALLOWED_PARAMS[self.protocol]))
        if unknown:
            raise ValueError(f"unsupported {self.protocol.value} parameters: {', '.join(unknown)}")
        missing = sorted(_REQUIRED_PARAMS[self.protocol].difference(params))
        if missing:
            raise ValueError(f"missing {self.protocol.value} parameters: {', '.join(missing)}")
        if self.protocol in {ProtocolFamily.EM_S, ProtocolFamily.EM_W}:
            if params.get("columns") != "ALL":
                raise ValueError("EM-S/EM-W production requests must preserve columns=ALL")
            _positive_int(params["pageNumber"], name="pageNumber")
            _positive_int(params["pageSize"], name="pageSize")
        elif self.protocol is ProtocolFamily.EM_M:
            _positive_int(params["p"], name="p")
            _positive_int(params["ps"], name="ps")
        elif self.protocol is ProtocolFamily.EM_F:
            if self.endpoint.endswith("/Index"):
                required = {"type", "code"}
            elif self.endpoint.endswith("DateAjaxNew"):
                required = {"companyType", "reportDateType", "code"}
            else:
                required = {
                    "companyType",
                    "reportDateType",
                    "reportType",
                    "dates",
                    "code",
                }
            if not required.issubset(params):
                missing = ", ".join(sorted(required.difference(params)))
                raise ValueError(f"missing EM-F endpoint parameters: {missing}")
            if not re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", params["code"], flags=re.I):
                raise ValueError("EM-F code must carry an explicit exchange")
            if self.endpoint.endswith("/Index") and params.get("type") != "web":
                raise ValueError("EM-F company-type discovery requires type=web")
            is_report = self.endpoint.endswith("AjaxNew") and not self.endpoint.endswith("DateAjaxNew")
            if is_report and not {"reportType", "dates"}.issubset(params):
                raise ValueError("EM-F report requests require reportType and dates")
        object.__setattr__(self, "params", params)


def _positive_int(value: str, *, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if number <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return number


def _http_failure(
    protocol: ProtocolFamily,
    *,
    body: bytes,
    status_code: int,
    page_number: int,
    diagnostic: str,
    business_code: str | None = None,
) -> ProtocolPage:
    proof = ExecutionProof(
        evidence_kind="http_response",
        status=ResultStatus.FAILED,
        response_sha256=hashlib.sha256(body).hexdigest(),
        rows_yielded=0,
        http_status=status_code,
        diagnostic=diagnostic,
    )
    return ProtocolPage(
        protocol=protocol,
        status=ResultStatus.FAILED,
        page_number=page_number,
        rows=(),
        declared_total=None,
        declared_pages=None,
        terminal=False,
        proof=proof,
        business_code=business_code,
        message=diagnostic,
    )


def parse_eastmoney_response(
    request: EastmoneyRequest,
    *,
    status_code: int,
    body: bytes,
    content_type: str | None = "application/json",
) -> ProtocolPage:
    """Classify a bounded Eastmoney response without treating error pages as empty."""

    if not 100 <= status_code <= 599:
        raise ValueError("invalid HTTP status")
    page_number = int(request.params.get("pageNumber", request.params.get("p", "1")))
    if len(body) > request.max_response_bytes:
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="response_too_large",
        )
    if status_code != 200:
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic=f"http_status:{status_code}",
        )
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in {"text/html", "application/xhtml+xml"}:
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="unexpected_html",
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="invalid_json",
        )
    if not isinstance(payload, Mapping):
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="invalid_payload_shape",
        )
    if request.protocol in {ProtocolFamily.EM_S, ProtocolFamily.EM_W, ProtocolFamily.EM_M}:
        return _parse_eastmoney_paginated(request, payload, body, status_code, page_number)
    if request.protocol is ProtocolFamily.EM_F:
        return _parse_em_f(request, payload, body, status_code)
    if request.protocol is ProtocolFamily.EM_Q:
        return _parse_em_q(request, payload, body, status_code)
    raise AssertionError("unreachable protocol")


class _CompanyTypeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.company_type: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id") == "hidctype" and attributes.get("value"):
            self.company_type = str(attributes["value"])


@dataclass(frozen=True, slots=True)
class EMFCompanyTypeEvidence:
    company_code: str
    company_type: str | None
    proof: ExecutionProof


def parse_em_f_company_type_response(
    request: EastmoneyRequest,
    *,
    status_code: int,
    body: bytes,
    content_type: str | None = "text/html",
) -> EMFCompanyTypeEvidence:
    """Parse the bounded EM-F HTML prerequisite without issuing hidden report I/O."""

    if request.protocol is not ProtocolFamily.EM_F or not request.endpoint.endswith("/Index"):
        raise ValueError("company type evidence requires the fixed EM-F Index request")
    digest = hashlib.sha256(body).hexdigest()
    diagnostic: str | None = None
    company_type: str | None = None
    if len(body) > request.max_response_bytes:
        diagnostic = "response_too_large"
    elif status_code != 200:
        diagnostic = f"http_status:{status_code}"
    elif (content_type or "").split(";", 1)[0].strip().lower() not in {
        "text/html",
        "application/xhtml+xml",
    }:
        diagnostic = "unexpected_company_type_mime"
    else:
        try:
            parser = _CompanyTypeParser()
            parser.feed(body.decode("utf-8"))
            company_type = parser.company_type
        except (UnicodeDecodeError, ValueError):
            diagnostic = "invalid_company_type_html"
        if not company_type:
            diagnostic = diagnostic or "company_type_missing"
    status = ResultStatus.SUCCESS if company_type else ResultStatus.FAILED
    proof = ExecutionProof(
        evidence_kind="http_response",
        status=status,
        response_sha256=digest,
        rows_yielded=1 if company_type else 0,
        http_status=status_code,
        diagnostic=diagnostic,
    )
    return EMFCompanyTypeEvidence(
        company_code=request.params["code"].upper(),
        company_type=company_type,
        proof=proof,
    )


def _business_code(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("code", payload.get("rc"))
    return None if value is None else str(value)


def _parse_eastmoney_paginated(
    request: EastmoneyRequest,
    payload: Mapping[str, Any],
    body: bytes,
    status_code: int,
    page_number: int,
) -> ProtocolPage:
    code = _business_code(payload)
    success = payload.get("success") is True and code in {None, "0"}
    if not success:
        if code == "9201" and payload.get("result") is None:
            proof = ExecutionProof(
                evidence_kind="http_response",
                status=ResultStatus.EMPTY,
                response_sha256=hashlib.sha256(body).hexdigest(),
                rows_yielded=0,
                http_status=status_code,
                diagnostic="eastmoney_9201_empty",
            )
            return ProtocolPage(
                protocol=request.protocol,
                status=ResultStatus.EMPTY,
                page_number=page_number,
                rows=(),
                declared_total=0,
                declared_pages=0,
                terminal=True,
                proof=proof,
                business_code=code,
                message=str(payload.get("message") or "返回数据为空"),
            )
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="eastmoney_business_failure",
            business_code=code,
        )
    result = payload.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("data"), list):
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="missing_paginated_result",
            business_code=code,
        )
    rows = result["data"]
    if any(not isinstance(row, Mapping) for row in rows):
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="invalid_result_row",
            business_code=code,
        )
    try:
        declared_total = int(result["count"])
        declared_pages = int(result["pages"])
    except (KeyError, TypeError, ValueError):
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="missing_pagination_totals",
            business_code=code,
        )
    if declared_total < 0 or declared_pages < 0:
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="invalid_pagination_totals",
            business_code=code,
        )
    status = ResultStatus.EMPTY if not rows and declared_total == 0 else ResultStatus.SUCCESS
    if not rows and declared_total > 0:
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=page_number,
            diagnostic="unexpected_empty_page",
            business_code=code,
        )
    terminal = declared_pages == 0 or page_number >= declared_pages
    proof = ExecutionProof(
        evidence_kind="http_response",
        status=status,
        response_sha256=hashlib.sha256(body).hexdigest(),
        rows_yielded=len(rows),
        http_status=status_code,
    )
    return ProtocolPage(
        protocol=request.protocol,
        status=status,
        page_number=page_number,
        rows=tuple(dict(row) for row in rows),
        declared_total=declared_total,
        declared_pages=declared_pages,
        terminal=terminal,
        proof=proof,
        business_code=code,
        message=str(payload.get("message")) if payload.get("message") is not None else None,
    )


def _parse_em_f(
    request: EastmoneyRequest,
    payload: Mapping[str, Any],
    body: bytes,
    status_code: int,
) -> ProtocolPage:
    # EM-F uses a metadata-only envelope (typically ``{"$type": ...}``) to
    # represent a valid batch with no disclosed rows.  Treat it exactly like
    # ``data: []``; malformed shapes that mention any other key remain errors.
    if "data" not in payload and set(payload).issubset({"$type", "$types"}):
        proof = ExecutionProof(
            evidence_kind="http_response",
            status=ResultStatus.EMPTY,
            response_sha256=hashlib.sha256(body).hexdigest(),
            rows_yielded=0,
            http_status=status_code,
        )
        return ProtocolPage(
            protocol=request.protocol,
            status=ResultStatus.EMPTY,
            page_number=1,
            rows=(),
            declared_total=0,
            declared_pages=0,
            terminal=True,
            proof=proof,
            business_code=_business_code(payload),
            message=str(payload.get("message")) if payload.get("message") is not None else None,
        )
    rows = payload.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=1,
            diagnostic="missing_financial_data",
            business_code=_business_code(payload),
        )
    status = ResultStatus.SUCCESS if rows else ResultStatus.EMPTY
    proof = ExecutionProof(
        evidence_kind="http_response",
        status=status,
        response_sha256=hashlib.sha256(body).hexdigest(),
        rows_yielded=len(rows),
        http_status=status_code,
    )
    return ProtocolPage(
        protocol=request.protocol,
        status=status,
        page_number=1,
        rows=tuple(dict(row) for row in rows),
        declared_total=len(rows),
        declared_pages=1 if rows else 0,
        terminal=True,
        proof=proof,
        business_code=_business_code(payload),
        message=str(payload.get("message")) if payload.get("message") is not None else None,
    )


def _parse_em_q(
    request: EastmoneyRequest,
    payload: Mapping[str, Any],
    body: bytes,
    status_code: int,
) -> ProtocolPage:
    code = _business_code(payload)
    if code != "0":
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=1,
            diagnostic="eastmoney_quote_failure",
            business_code=code,
        )
    data = payload.get("data")
    if data is None:
        rows: tuple[Mapping[str, Any], ...] = ()
        status = ResultStatus.EMPTY
    elif isinstance(data, Mapping):
        rows = (dict(data),)
        status = ResultStatus.SUCCESS
    else:
        return _http_failure(
            request.protocol,
            body=body,
            status_code=status_code,
            page_number=1,
            diagnostic="invalid_quote_shape",
            business_code=code,
        )
    proof = ExecutionProof(
        evidence_kind="http_response",
        status=status,
        response_sha256=hashlib.sha256(body).hexdigest(),
        rows_yielded=len(rows),
        http_status=status_code,
    )
    return ProtocolPage(
        protocol=request.protocol,
        status=status,
        page_number=1,
        rows=rows,
        declared_total=len(rows),
        declared_pages=1 if rows else 0,
        terminal=True,
        proof=proof,
        business_code=code,
    )


@dataclass(frozen=True, slots=True)
class EMFPrerequisite:
    kind: str
    statement: str | None = None


@dataclass(frozen=True, slots=True)
class EMFReportBatch:
    dataset_id: str
    statement: str
    report_type: str
    company_code: str
    company_type: str
    dates: tuple[date, ...]

    @property
    def params(self) -> Mapping[str, str]:
        return {
            "companyType": self.company_type,
            "reportDateType": "0",
            "reportType": self.report_type,
            "dates": ",".join(item.isoformat() for item in self.dates),
            "code": self.company_code,
        }


@dataclass(frozen=True, slots=True)
class EMFPlan:
    company_code: str
    company_type: str | None
    prerequisites: tuple[EMFPrerequisite, ...]
    report_batches: tuple[EMFReportBatch, ...]
    catalog_dates: Mapping[str, tuple[date, ...]] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return not self.prerequisites


_EMF_DATASETS: Mapping[str, tuple[str, str]] = {
    "balance_fields": ("balance", "1"),
    "income_fields": ("income", "1"),
    "cashflow_fields": ("cashflow", "1"),
    "income_quarter": ("income", "2"),
    "cashflow_quarter": ("cashflow", "2"),
}


def plan_em_f(
    *,
    company_code: str,
    company_type: str | None,
    catalogs: Mapping[str, Sequence[date]] | None,
    dataset_ids: Sequence[str] = tuple(_EMF_DATASETS),
    requested_dates: Iterable[date] | None = None,
    batch_size: int = 5,
) -> EMFPlan:
    """Plan explicit prerequisite and report work; this function performs zero I/O."""

    if not re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", company_code, flags=re.I):
        raise ValueError("company_code must carry an explicit exchange")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    unknown = sorted(set(dataset_ids).difference(_EMF_DATASETS))
    if unknown:
        raise ValueError(f"unknown EM-F datasets: {', '.join(unknown)}")
    if not company_type:
        return EMFPlan(
            company_code=company_code.upper(),
            company_type=None,
            prerequisites=(EMFPrerequisite("company_type"),),
            report_batches=(),
        )

    catalog_map = dict(catalogs or {})
    required_statements = tuple(dict.fromkeys(_EMF_DATASETS[item][0] for item in dataset_ids))
    missing = [statement for statement in required_statements if statement not in catalog_map]
    if missing:
        return EMFPlan(
            company_code=company_code.upper(),
            company_type=str(company_type),
            prerequisites=tuple(
                EMFPrerequisite("report_date_catalog", statement) for statement in missing
            ),
            report_batches=(),
            catalog_dates={
                key: tuple(sorted(set(value), reverse=True))
                for key, value in catalog_map.items()
            },
        )

    wanted = set(requested_dates) if requested_dates is not None else None
    normalized_catalogs: dict[str, tuple[date, ...]] = {}
    for statement in required_statements:
        values = tuple(sorted(set(catalog_map[statement]), reverse=True))
        if wanted is not None:
            absent = wanted.difference(values)
            if absent:
                missing_dates = ", ".join(sorted(item.isoformat() for item in absent))
                raise ValueError(f"requested report dates are absent from {statement} catalog: {missing_dates}")
            values = tuple(item for item in values if item in wanted)
        normalized_catalogs[statement] = values

    batches: list[EMFReportBatch] = []
    for dataset_id in dataset_ids:
        statement, report_type = _EMF_DATASETS[dataset_id]
        dates = normalized_catalogs[statement]
        for offset in range(0, len(dates), batch_size):
            batches.append(
                EMFReportBatch(
                    dataset_id=dataset_id,
                    statement=statement,
                    report_type=report_type,
                    company_code=company_code.upper(),
                    company_type=str(company_type),
                    dates=dates[offset : offset + batch_size],
                )
            )
    return EMFPlan(
        company_code=company_code.upper(),
        company_type=str(company_type),
        prerequisites=(),
        report_batches=tuple(batches),
        catalog_dates=normalized_catalogs,
    )


BAOSTOCK_METHOD_PARAMS: Mapping[str, frozenset[str]] = {
    "query_history_k_data_plus": frozenset(
        {"code", "fields", "start_date", "end_date", "frequency", "adjustflag"}
    ),
    "query_profit_data": frozenset({"code", "year", "quarter"}),
    "query_operation_data": frozenset({"code", "year", "quarter"}),
    "query_growth_data": frozenset({"code", "year", "quarter"}),
    "query_balance_data": frozenset({"code", "year", "quarter"}),
    "query_cash_flow_data": frozenset({"code", "year", "quarter"}),
    "query_dupont_data": frozenset({"code", "year", "quarter"}),
    "query_stock_basic": frozenset({"code", "code_name"}),
    "query_trade_dates": frozenset({"start_date", "end_date"}),
    "query_adjust_factor": frozenset({"code", "start_date", "end_date"}),
}


@dataclass(frozen=True, slots=True)
class BaoStockQuery:
    method: str
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.method not in BAOSTOCK_METHOD_PARAMS:
            raise ValueError("BaoStock method is not registered")
        unknown = sorted(set(self.params).difference(BAOSTOCK_METHOD_PARAMS[self.method]))
        if unknown:
            raise ValueError(f"unsupported BaoStock parameters: {', '.join(unknown)}")
        object.__setattr__(self, "params", dict(self.params))


@dataclass(frozen=True, slots=True)
class BaoStockResult:
    query: BaoStockQuery
    rows: tuple[Mapping[str, Any], ...]
    proof: ExecutionProof


class BaoStockSdk(Protocol):
    def login(self, user_id: str = "anonymous", password: str = "123456") -> Any: ...

    def logout(self, user_id: str = "anonymous") -> Any: ...


@contextmanager
def baostock_anonymous_session(sdk: BaoStockSdk) -> Iterator[None]:
    login = sdk.login()
    if str(getattr(login, "error_code", "")) != "0":
        raise ProtocolError(f"BaoStock login failed: {getattr(login, 'error_msg', '')}")
    try:
        yield
    finally:
        sdk.logout()


def execute_baostock_query(
    sdk: Any,
    query: BaoStockQuery,
    *,
    max_rows: int = 100_000,
) -> BaoStockResult:
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
    result = getattr(sdk, query.method)(**query.params)
    fields = tuple(str(item) for item in getattr(result, "fields", ()))
    initial_code = str(getattr(result, "error_code", ""))
    if initial_code != "0":
        return _sdk_failure(query, code=initial_code, rows=(), diagnostic=str(getattr(result, "error_msg", "")))

    rows: list[Mapping[str, Any]] = []
    try:
        while result.next():
            if len(rows) >= max_rows:
                return _sdk_partial(query, code=str(getattr(result, "error_code", "0")), rows=rows, diagnostic="row_limit_exhausted")
            raw_row = result.get_row_data()
            if isinstance(raw_row, Mapping):
                row = dict(raw_row)
            else:
                values = tuple(raw_row)
                if len(values) != len(fields):
                    return _sdk_partial(query, code="schema", rows=rows, diagnostic="field_count_mismatch")
                row = dict(zip(fields, values))
            rows.append(row)
    except Exception as exc:  # injected SDK boundary: preserve any completed rows
        return _sdk_partial(query, code=str(getattr(result, "error_code", "exception")), rows=rows, diagnostic=f"iteration_exception:{type(exc).__name__}")

    final_code = str(getattr(result, "error_code", ""))
    if final_code != "0":
        if rows:
            return _sdk_partial(query, code=final_code, rows=rows, diagnostic=str(getattr(result, "error_msg", "")))
        return _sdk_failure(query, code=final_code, rows=(), diagnostic=str(getattr(result, "error_msg", "")))
    status = ResultStatus.SUCCESS if rows else ResultStatus.EMPTY
    proof = _sdk_proof(query, status=status, code="0", rows=rows, exhausted=True)
    return BaoStockResult(query=query, rows=tuple(rows), proof=proof)


def execute_baostock_queries(
    sdk: BaoStockSdk,
    queries: Sequence[BaoStockQuery],
    *,
    max_rows_per_query: int = 100_000,
) -> tuple[BaoStockResult, ...]:
    with baostock_anonymous_session(sdk):
        return tuple(
            execute_baostock_query(sdk, query, max_rows=max_rows_per_query)
            for query in queries
        )


def _sdk_digest(query: BaoStockQuery, rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        {"method": query.method, "params": query.params, "rows": rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sdk_proof(
    query: BaoStockQuery,
    *,
    status: ResultStatus,
    code: str,
    rows: Sequence[Mapping[str, Any]],
    exhausted: bool,
    diagnostic: str | None = None,
) -> ExecutionProof:
    return ExecutionProof(
        evidence_kind="sdk_result",
        status=status,
        response_sha256=_sdk_digest(query, rows),
        rows_yielded=len(rows),
        sdk_code=code,
        result_set_exhausted=exhausted,
        diagnostic=diagnostic,
    )


def _sdk_failure(
    query: BaoStockQuery,
    *,
    code: str,
    rows: Sequence[Mapping[str, Any]],
    diagnostic: str,
) -> BaoStockResult:
    return BaoStockResult(
        query=query,
        rows=tuple(rows),
        proof=_sdk_proof(
            query,
            status=ResultStatus.FAILED,
            code=code,
            rows=rows,
            exhausted=False,
            diagnostic=diagnostic,
        ),
    )


def _sdk_partial(
    query: BaoStockQuery,
    *,
    code: str,
    rows: Sequence[Mapping[str, Any]],
    diagnostic: str,
) -> BaoStockResult:
    return BaoStockResult(
        query=query,
        rows=tuple(rows),
        proof=_sdk_proof(
            query,
            status=ResultStatus.PARTIAL,
            code=code,
            rows=rows,
            exhausted=False,
            diagnostic=diagnostic,
        ),
    )


_FINANCIAL_METHODS = frozenset(
    {
        "query_profit_data",
        "query_operation_data",
        "query_growth_data",
        "query_balance_data",
        "query_cash_flow_data",
        "query_dupont_data",
    }
)


def build_baostock_financial_batches(
    *,
    method: str,
    code: str,
    start_year: int,
    end_year: int,
    quarters: Sequence[int] = (1, 2, 3, 4),
) -> tuple[BaoStockQuery, ...]:
    if method not in _FINANCIAL_METHODS:
        raise ValueError("method is not a BaoStock financial query")
    if start_year > end_year:
        raise ValueError("start_year cannot follow end_year")
    if not quarters or any(item not in {1, 2, 3, 4} for item in quarters):
        raise ValueError("quarters must contain values from 1 to 4")
    return tuple(
        BaoStockQuery(method, {"code": code, "year": year, "quarter": quarter})
        for year in range(start_year, end_year + 1)
        for quarter in quarters
    )


def build_baostock_date_batches(
    *,
    method: str,
    start_date: date,
    end_date: date,
    max_days: int,
    base_params: Mapping[str, Any] | None = None,
) -> tuple[BaoStockQuery, ...]:
    if method not in {
        "query_history_k_data_plus",
        "query_trade_dates",
        "query_adjust_factor",
    }:
        raise ValueError("method is not a date-range BaoStock query")
    if start_date > end_date:
        raise ValueError("start_date cannot follow end_date")
    if max_days <= 0:
        raise ValueError("max_days must be positive")
    batches: list[BaoStockQuery] = []
    cursor = start_date
    while cursor <= end_date:
        batch_end = min(end_date, cursor + timedelta(days=max_days - 1))
        params = dict(base_params or {})
        params.update({"start_date": cursor.isoformat(), "end_date": batch_end.isoformat()})
        batches.append(BaoStockQuery(method, params))
        cursor = batch_end + timedelta(days=1)
    return tuple(batches)
