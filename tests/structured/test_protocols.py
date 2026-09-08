from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from analysis.acquisition.models import AttemptKind, PhysicalQueryPlanItem
from analysis.structured.protocols import (
    BAOSTOCK_METHOD_PARAMS,
    EASTMONEY_ENDPOINTS,
    BaoStockQuery,
    EastmoneyRequest,
    ExecutionProof,
    ProtocolError,
    ProtocolFamily,
    ResultStatus,
    build_baostock_date_batches,
    build_baostock_financial_batches,
    execute_baostock_queries,
    execute_baostock_query,
    parse_eastmoney_response,
    parse_em_f_company_type_response,
    plan_em_f,
)


def _request(protocol: ProtocolFamily, *, page: int = 1, bound: int = 10_000) -> EastmoneyRequest:
    endpoint = next(iter(EASTMONEY_ENDPOINTS[protocol]))
    if protocol in {ProtocolFamily.EM_S, ProtocolFamily.EM_W}:
        params = {
            "reportName": "RPT_TEST",
            "columns": "ALL",
            "filter": '(SECUCODE="600519.SH")',
            "pageNumber": str(page),
            "pageSize": "2",
            "sortColumns": "REPORT_DATE,ITEM_CODE",
            "sortTypes": "-1,1",
            "source": "HSF10",
            "client": "PC",
        }
    elif protocol is ProtocolFamily.EM_M:
        params = {
            "type": "RPT_F10_FINANCE_MAINFINADATA",
            "sty": "APP_F10_MAINFINADATA",
            "filter": '(SECUCODE="600519.SH")',
            "p": str(page),
            "ps": "2",
            "sr": "-1",
            "st": "REPORT_DATE",
            "source": "HSF10",
            "client": "PC",
        }
    elif protocol is ProtocolFamily.EM_F:
        endpoint = next(item for item in EASTMONEY_ENDPOINTS[protocol] if item.endswith("lrbAjaxNew"))
        params = {
            "companyType": "4",
            "reportDateType": "0",
            "reportType": "1",
            "dates": "2025-12-31",
            "code": "SH600519",
        }
    else:
        params = {"secid": "1.600519", "fields": "f57,f58,f86,f116,f117"}
    return EastmoneyRequest(protocol, endpoint, params, max_response_bytes=bound)


def _body(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()


def test_em_s_multi_page_and_terminal_classification() -> None:
    first = parse_eastmoney_response(
        _request(ProtocolFamily.EM_S, page=1),
        status_code=200,
        body=_body({"success": True, "code": 0, "result": {"data": [{"id": 1}, {"id": 2}], "count": 3, "pages": 2}}),
    )
    second = parse_eastmoney_response(
        _request(ProtocolFamily.EM_S, page=2),
        status_code=200,
        body=_body({"success": True, "code": 0, "result": {"data": [{"id": 3}], "count": 3, "pages": 2}}),
    )
    assert first.status is ResultStatus.SUCCESS and not first.terminal
    assert second.terminal and second.declared_total == 3


@pytest.mark.parametrize("protocol", [ProtocolFamily.EM_W, ProtocolFamily.EM_M])
def test_paginated_protocols_share_strict_result_shape(protocol: ProtocolFamily) -> None:
    result = parse_eastmoney_response(
        _request(protocol),
        status_code=200,
        body=_body({"success": True, "code": "0", "result": {"data": [{"x": 1}], "count": 1, "pages": 1}}),
    )
    assert result.terminal and result.rows == ({"x": 1},)


def test_9201_is_bounded_empty_evidence_not_no_event_fact() -> None:
    result = parse_eastmoney_response(
        _request(ProtocolFamily.EM_W),
        status_code=200,
        body=_body({"success": False, "code": 9201, "result": None, "message": "返回数据为空"}),
    )
    assert result.status is ResultStatus.EMPTY
    assert result.business_code == "9201"
    assert result.proof.http_status == 200
    assert result.declared_total == 0


@pytest.mark.parametrize(
    ("body", "content_type", "diagnostic"),
    [
        (_body({"success": False, "code": 9999, "result": None}), "application/json", "eastmoney_business_failure"),
        (b"<html>captcha</html>", "text/html", "unexpected_html"),
        (b"not-json", "application/json", "invalid_json"),
        (_body({"success": True, "code": 0, "result": {"data": []}}), "application/json", "missing_pagination_totals"),
    ],
)
def test_http_200_error_payloads_never_become_empty(body: bytes, content_type: str, diagnostic: str) -> None:
    result = parse_eastmoney_response(
        _request(ProtocolFamily.EM_S), status_code=200, body=body, content_type=content_type
    )
    assert result.status is ResultStatus.FAILED
    assert result.proof.diagnostic == diagnostic


def test_http_response_bound_is_enforced_before_parsing() -> None:
    result = parse_eastmoney_response(
        _request(ProtocolFamily.EM_S, bound=4), status_code=200, body=b"12345"
    )
    assert result.status is ResultStatus.FAILED
    assert result.proof.diagnostic == "response_too_large"


def test_http_zero_result_contract_stays_structured() -> None:
    result = parse_eastmoney_response(
        _request(ProtocolFamily.EM_S),
        status_code=200,
        body=_body({"success": True, "code": 0, "result": {"data": [], "count": 0, "pages": 0}}),
    )
    assert result.status is ResultStatus.EMPTY and result.terminal


def test_nonzero_declared_total_cannot_hide_an_empty_page() -> None:
    result = parse_eastmoney_response(
        _request(ProtocolFamily.EM_S),
        status_code=200,
        body=_body({"success": True, "code": 0, "result": {"data": [], "count": 10, "pages": 5}}),
    )
    assert result.status is ResultStatus.FAILED
    assert result.proof.diagnostic == "unexpected_empty_page"


def test_em_f_and_em_q_have_distinct_shapes() -> None:
    statement = parse_eastmoney_response(
        _request(ProtocolFamily.EM_F), status_code=200, body=_body({"data": [{"REPORT_DATE": "2025-12-31"}]})
    )
    quote = parse_eastmoney_response(
        _request(ProtocolFamily.EM_Q), status_code=200, body=_body({"rc": 0, "data": {"f116": 1}})
    )
    assert statement.rows[0]["REPORT_DATE"] == "2025-12-31"
    assert quote.rows == ({"f116": 1},)


def test_em_f_company_type_prerequisite_uses_bounded_html_evidence() -> None:
    endpoint = next(
        item for item in EASTMONEY_ENDPOINTS[ProtocolFamily.EM_F] if item.endswith("/Index")
    )
    request = EastmoneyRequest(
        ProtocolFamily.EM_F,
        endpoint,
        {"type": "web", "code": "SZ000001"},
    )
    evidence = parse_em_f_company_type_response(
        request,
        status_code=200,
        body=b'<html><input value="3" id="hidctype"></html>',
    )
    assert evidence.company_type == "3"
    assert evidence.proof.http_status == 200


def test_em_f_company_type_missing_is_a_failed_prerequisite() -> None:
    endpoint = next(
        item for item in EASTMONEY_ENDPOINTS[ProtocolFamily.EM_F] if item.endswith("/Index")
    )
    request = EastmoneyRequest(
        ProtocolFamily.EM_F,
        endpoint,
        {"type": "web", "code": "SH600519"},
    )
    evidence = parse_em_f_company_type_response(
        request, status_code=200, body=b"<html></html>"
    )
    assert evidence.company_type is None
    assert evidence.proof.status is ResultStatus.FAILED
    assert evidence.proof.diagnostic == "company_type_missing"


def test_request_parameter_and_endpoint_allowlists_fail_closed() -> None:
    base = _request(ProtocolFamily.EM_S)
    with pytest.raises(ValueError, match="unsupported"):
        EastmoneyRequest(base.protocol, base.endpoint, {**base.params, "callback": "unsafe"})
    with pytest.raises(ValueError, match="allowlist"):
        EastmoneyRequest(base.protocol, "https://example.com/", base.params)
    with pytest.raises(ValueError, match="columns=ALL"):
        EastmoneyRequest(base.protocol, base.endpoint, {**base.params, "columns": "A,B"})


def test_em_f_prerequisites_prevent_report_work() -> None:
    no_type = plan_em_f(company_code="SH600519", company_type=None, catalogs=None)
    assert not no_type.ready and no_type.report_batches == ()
    no_catalog = plan_em_f(company_code="SH600519", company_type="4", catalogs={})
    assert not no_catalog.ready and no_catalog.report_batches == ()
    assert {item.statement for item in no_catalog.prerequisites} == {"balance", "income", "cashflow"}


def test_em_f_catalog_is_not_truncated_and_company_type_is_not_hardcoded() -> None:
    dates = tuple(date(2025 - offset // 4, (4 - offset % 4) * 3, 31 if offset % 4 in {0, 3} else 30) for offset in range(20))
    plan = plan_em_f(
        company_code="SZ000001",
        company_type="3",
        catalogs={"balance": dates, "income": dates, "cashflow": dates},
        batch_size=5,
    )
    assert plan.ready
    assert all(batch.company_type == "3" for batch in plan.report_batches)
    assert sum(len(batch.dates) for batch in plan.report_batches if batch.dataset_id == "balance_fields") == 20


def test_em_f_requested_date_must_come_from_persisted_catalog() -> None:
    with pytest.raises(ValueError, match="absent"):
        plan_em_f(
            company_code="SH600519",
            company_type="4",
            catalogs={"balance": [date(2025, 12, 31)]},
            dataset_ids=("balance_fields",),
            requested_dates=[date(2024, 12, 31)],
        )


class FakeResult:
    def __init__(self, fields=(), rows=(), code="0", fail_after=None):
        self.fields = list(fields)
        self._rows = list(rows)
        self._index = 0
        self.error_code = code
        self.error_msg = "failure" if code != "0" else ""
        self.fail_after = fail_after

    def next(self):
        if self.fail_after is not None and self._index >= self.fail_after:
            self.error_code = "10002007"
            self.error_msg = "iteration interrupted"
            return False
        return self._index < len(self._rows)

    def get_row_data(self):
        row = self._rows[self._index]
        self._index += 1
        return row


class FakeSdk:
    def __init__(self, result, *, login_code="0", query_error=None):
        self.result = result
        self.login_code = login_code
        self.query_error = query_error
        self.logins = 0
        self.logouts = 0

    def login(self):
        self.logins += 1
        return SimpleNamespace(error_code=self.login_code, error_msg="login failed")

    def logout(self):
        self.logouts += 1

    def query_profit_data(self, **params):
        if self.query_error:
            raise self.query_error
        return self.result


def _profit_query() -> BaoStockQuery:
    return BaoStockQuery("query_profit_data", {"code": "sh.600519", "year": 2025, "quarter": 4})


def test_baostock_success_and_valid_empty_require_full_exhaustion() -> None:
    success = execute_baostock_query(FakeSdk(FakeResult(["code", "roeAvg"], [["sh.600519", "0.3"]])), _profit_query())
    empty = execute_baostock_query(FakeSdk(FakeResult(["code"], [])), _profit_query())
    assert success.proof.status is ResultStatus.SUCCESS
    assert success.proof.result_set_exhausted is True
    assert empty.proof.status is ResultStatus.EMPTY and empty.rows == ()
    assert success.proof.http_status is None and empty.proof.http_status is None


def test_baostock_failure_and_partial_iteration_are_distinct() -> None:
    failed = execute_baostock_query(FakeSdk(FakeResult(code="1001")), _profit_query())
    partial = execute_baostock_query(
        FakeSdk(FakeResult(["code"], [["a"], ["b"]], fail_after=1)), _profit_query()
    )
    assert failed.proof.status is ResultStatus.FAILED and failed.rows == ()
    assert partial.proof.status is ResultStatus.PARTIAL
    assert partial.rows == ({"code": "a"},)
    assert partial.proof.result_set_exhausted is False


def test_baostock_row_bound_and_schema_mismatch_remain_partial() -> None:
    bounded = execute_baostock_query(
        FakeSdk(FakeResult(["code"], [["a"], ["b"]])), _profit_query(), max_rows=1
    )
    mismatch = execute_baostock_query(
        FakeSdk(FakeResult(["code", "x"], [["a"]])), _profit_query()
    )
    assert bounded.proof.diagnostic == "row_limit_exhausted"
    assert mismatch.proof.diagnostic == "field_count_mismatch"


def test_anonymous_session_always_closes_after_query_exception() -> None:
    sdk = FakeSdk(FakeResult(), query_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        execute_baostock_queries(sdk, [_profit_query()])
    assert sdk.logins == 1 and sdk.logouts == 1


def test_login_failure_issues_no_query_and_no_fake_http() -> None:
    sdk = FakeSdk(FakeResult(), login_code="10001001")
    with pytest.raises(ProtocolError, match="login failed"):
        execute_baostock_queries(sdk, [_profit_query()])
    assert sdk.logins == 1 and sdk.logouts == 0
    with pytest.raises(ValueError, match="cannot forge"):
        ExecutionProof(
            evidence_kind="sdk_result",
            status=ResultStatus.SUCCESS,
            response_sha256="0" * 64,
            rows_yielded=1,
            http_status=200,
            sdk_code="0",
            result_set_exhausted=True,
        )


def test_all_ten_baostock_methods_have_executable_query_contracts() -> None:
    assert len(BAOSTOCK_METHOD_PARAMS) == 10
    assert "query_history_k_data_plus" in BAOSTOCK_METHOD_PARAMS
    assert "query_adjust_factor" in BAOSTOCK_METHOD_PARAMS


def test_baostock_financial_and_date_batches_cover_full_bounds() -> None:
    finance = build_baostock_financial_batches(
        method="query_profit_data", code="sh.600519", start_year=2024, end_year=2025
    )
    dates = build_baostock_date_batches(
        method="query_trade_dates",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 10),
        max_days=4,
    )
    assert len(finance) == 8
    assert [(item.params["start_date"], item.params["end_date"]) for item in dates] == [
        ("2025-01-01", "2025-01-04"),
        ("2025-01-05", "2025-01-08"),
        ("2025-01-09", "2025-01-10"),
    ]


def test_physical_plan_item_preserves_native_sdk_contract() -> None:
    item = PhysicalQueryPlanItem(
        plan_item_id="plan-item-sdk",
        run_id="run-sdk",
        source_definition_id="structured-baostock",
        source_definition_version="1.0.0",
        query_id="query-history-k-data-plus",
        query_family="baostock-sdk",
        execution_key="baostock-history",
        attempt_kind=AttemptKind.DISCOVERY,
        request_method="SDK",
        endpoint="baostock+sdk://query_history_k_data_plus",
        normalized_parameters={"code": "sh.600519"},
        partition_key="600519:market",
        pagination_fingerprint="sdk-exhaustion-v1",
        ordinal=0,
        time_start="2026-09-08T00:00:00+00:00",
        time_end="2026-09-09T00:00:00+00:00",
    )

    assert item.request_method == "SDK"
    assert item.request_encoding == "sdk"
