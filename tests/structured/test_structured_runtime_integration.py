from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.structured.runtime import FrozenSource
from analysis.structured.service import StructuredDataService


NOW = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)


class NoWaitSourceGate:
    @contextmanager
    def hold(self, *_args, **_kwargs):
        yield SimpleNamespace()


def _acquisition_runtime(tmp_path, *, handler=None):
    client = httpx.Client(
        transport=httpx.MockTransport(
            handler
            or (lambda _request: httpx.Response(500, json={"error": "unused"}))
        )
    )
    runtime = AcquisitionRuntime.create(
        tmp_path / "isolated.db",
        tmp_path / "data",
        http_client=client,
        clock=lambda: datetime.now(timezone.utc),
        monotonic_clock=time.monotonic,
        sleeper=lambda _seconds: None,
    )
    runtime.source_gate = NoWaitSourceGate()
    return runtime, client


def test_from_runtime_reuses_one_control_plane_and_freezes_55_queries(tmp_path):
    acquisition_runtime, client = _acquisition_runtime(tmp_path)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime)

        assert service.acquisition_runtime is acquisition_runtime
        assert service.runtime.repository is acquisition_runtime.repository
        assert service.runtime.acquisition_runtime.snapshot_service is (
            acquisition_runtime.snapshot_service
        )
        assert service.storage.storage_namespace_id == acquisition_runtime.namespace_id
        assert sum(len(source.queries) for source in service.runtime._sources.values()) == 55
        assert all(
            isinstance(FrozenSource.from_mapping(source.to_mapping()), FrozenSource)
            for source in service.runtime._sources.values()
        )
        eastmoney, baostock = {
            source.upstream_identity: source
            for source in service.runtime._sources.values()
        }.values()
        assert all(query.request_method == "GET" for query in eastmoney.queries)
        assert all(query.endpoint.startswith("https://") for query in eastmoney.queries)
        assert all(query.request_method == "SDK" for query in baostock.queries)
        assert all(
            query.endpoint.startswith("baostock+sdk://")
            for query in baostock.queries
        )

        # A borrowed runtime remains owned by the caller.
        service.close()
        assert client.is_closed is False
    finally:
        acquisition_runtime.close()
        client.close()


def test_plan_is_durable_idempotent_and_empty_dataset_list_means_all(tmp_path):
    acquisition_runtime, client = _acquisition_runtime(tmp_path)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime)
        first = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("company_basic",),
            as_of=NOW,
        )
        repeated = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("company_basic",),
            as_of=NOW,
        )

        assert first["persisted"] is True
        assert first["performed_network_io"] is False
        assert first["created"] is True
        assert repeated["created"] is False
        assert repeated["run_ids"] == first["run_ids"]
        run_id = first["run_ids"][0]
        context = service.storage.get_run_context(run_id)
        assert context.frozen_config["datasets"][0]["dataset_id"] == "company_basic"
        assert service.status(run_id)["pending"] == 1
    finally:
        acquisition_runtime.close()
        client.close()

    all_runtime, all_client = _acquisition_runtime(tmp_path / "all")
    try:
        all_service = StructuredDataService.from_runtime(all_runtime)
        with pytest.raises(ValueError, match="incremental需要每个适用数据集都有安全coverage"):
            all_service.plan("600519", mode="incremental", as_of=NOW)
    finally:
        all_runtime.close()
        all_client.close()


def test_http_page_is_snapshotted_then_projected_under_shared_run(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "datacenter.eastmoney.com"
        assert request.url.params["pageNumber"] == "1"
        assert request.url.params["filter"] == '(SECUCODE="600519.SH")'
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "result": {
                    "data": [
                        {
                            "SECUCODE": "600519.SH",
                            "ORG_CODE": "1000000000",
                            "ORG_NAME": "贵州茅台",
                        }
                    ],
                    "count": 1,
                    "pages": 1,
                },
            },
        )

    acquisition_runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("company_basic",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]

        result = service.run(run_id)

        assert len(result["attempted_job_ids"]) == 1
        assert result["status"]["succeeded"] == 1
        job = service.storage.list_jobs(run_id, limit=None)[0]
        pages = service.storage.list_pages(job["job_id"])
        records = service.storage.list_records(job_id=job["job_id"], limit=None)
        fields = service.storage.list_record_fields(
            record_version_id=records[0]["record_version_id"], limit=None
        )
        coverage = service.storage.list_acquisition_coverage(
            run_id=run_id, limit=None
        )
        snapshot = acquisition_runtime.repository.get_raw_resource_snapshot(
            pages[0]["snapshot_id"]
        )
        assert pages[0]["terminal"] is True
        assert records[0]["raw_row"]["__retrieved_at"].endswith("+00:00")
        assert {item["raw_field_name"] for item in fields} >= {
            "SECUCODE",
            "ORG_CODE",
            "ORG_NAME",
        }
        assert coverage[0]["status"] == "complete"
        assert snapshot.physical_query_plan_item_id == job["plan_item_id"]
    finally:
        acquisition_runtime.close()
        client.close()


def test_em_f_prerequisites_expand_catalog_into_persisted_report_batches(tmp_path):
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path.endswith("/Index"):
            assert dict(request.url.params) == {
                "type": "web",
                "code": "SH600519",
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b'<html><input id="hidctype" value="4"></html>',
            )
        if request.url.path.endswith("zcfzbDateAjaxNew"):
            assert request.url.params["companyType"] == "4"
            assert "dates" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": [
                        {
                            "SECURITY_CODE": "600519",
                            "REPORT_DATE": "2025-12-31",
                            "REPORT_TYPE": "年报",
                        },
                        {
                            "SECURITY_CODE": "600519",
                            "REPORT_DATE": "2025-09-30",
                            "REPORT_TYPE": "三季报",
                        },
                    ],
                },
            )
        assert request.url.path.endswith("zcfzbAjaxNew")
        assert request.url.params["companyType"] == "4"
        assert request.url.params["reportType"] == "1"
        assert request.url.params["dates"] == "2025-12-31,2025-09-30"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "SECUCODE": "600519.SH",
                        "REPORT_DATE": "2025-12-31",
                        "ORG_CODE": "1000000000",
                        "TOTAL_ASSETS": 1,
                    },
                    {
                        "SECUCODE": "600519.SH",
                        "REPORT_DATE": "2025-09-30",
                        "ORG_CODE": "1000000000",
                        "TOTAL_ASSETS": 2,
                    },
                ],
            },
        )

    acquisition_runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("balance_fields",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        assert [
            item["purpose"]
            for item in service.storage.list_jobs(run_id, limit=None)
        ] == ["company_type", "report_catalog"]

        service.run(run_id)
        service.resume(run_id)
        jobs = service.storage.list_jobs(run_id, limit=None)
        assert [item["purpose"] for item in jobs] == [
            "company_type",
            "report_catalog",
            "report_period",
        ]
        service.resume(run_id)

        assert service.status(run_id)["succeeded"] == 3
        assert [url.split("?", 1)[0].rsplit("/", 1)[-1] for url in urls] == [
            "Index",
            "zcfzbDateAjaxNew",
            "zcfzbAjaxNew",
        ]
    finally:
        acquisition_runtime.close()
        client.close()


def _run_all_rounds(service: StructuredDataService, run_id: str) -> dict:
    result = service.run(run_id)
    for _ in range(12):
        status = service.status(run_id)
        if status["partial"]:
            return result
        if not status["pending"] and not status["retryable"] and not status["partial"]:
            return result
        result = service.resume(run_id)
    raise AssertionError("structured test run did not reach a terminal status")


def _emf_handler(period_rows, *, empty_envelope=False):
    catalog_dates = [
        "2025-12-31",
        "2025-09-30",
        "2025-06-30",
        "2025-03-31",
        "2024-12-31",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Index"):
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b'<html><input id="hidctype" value="4"></html>',
            )
        if request.url.path.endswith("lrbDateAjaxNew"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": [
                        {"SECURITY_CODE": "600519", "REPORT_DATE": value}
                        for value in catalog_dates
                    ],
                },
            )
        assert request.url.path.endswith("lrbAjaxNew")
        payload = {"$type": "Eastmoney.FinanceResult"} if empty_envelope else {"code": 0, "data": period_rows}
        return httpx.Response(200, json=payload)

    return handler, catalog_dates


def test_em_f_empty_envelope_is_no_data_and_records_absent_periods(tmp_path):
    handler, catalog_dates = _emf_handler([], empty_envelope=True)
    acquisition_runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("income_quarter",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        # Return the EM-F metadata-only empty envelope for the report batch.
        _run_all_rounds(service, run_id)
        report_job = next(
            item
            for item in service.storage.list_jobs(run_id, limit=None)
            if item["purpose"] == "report_period"
        )
        coverage = next(
            item
            for item in service.storage.list_acquisition_coverage(run_id=run_id, limit=None)
            if item.get("job_id") == report_job["job_id"]
        )
        attempt = next(
            item
            for item in acquisition_runtime.repository.list_attempts(run_id=run_id)
            if item.physical_query_plan_item_id == report_job["plan_item_id"]
        )
        events = acquisition_runtime.repository.list_attempt_events(attempt.attempt_id)
        terminal = next(item for item in events if item.outcome is not None)
        assert terminal.outcome.value == "no_data"
        assert "supplier_period_absent" in (terminal.reason_code or "")
        assert terminal.protocol_summary["absent_periods"] == sorted(catalog_dates)
        assert coverage["status"] == "no_data"
        assert coverage["absent_periods"] == sorted(catalog_dates)
    finally:
        acquisition_runtime.close()
        client.close()


@pytest.mark.parametrize(
    ("period_rows", "expected_absent"),
    [
        (
            [
                {"SECUCODE": "600519.SH", "REPORT_DATE": "2025-12-31", "ORG_CODE": "1", "NET_PROFIT": 1},
                {"SECUCODE": "600519.SH", "REPORT_DATE": "2025-09-30", "ORG_CODE": "1", "NET_PROFIT": 2},
            ],
            ["2024-12-31", "2025-03-31", "2025-06-30"],
        ),
        (
            [
                {"SECUCODE": "600519.SH", "REPORT_DATE": value, "ORG_CODE": "1", "NET_PROFIT": 1}
                for value in ["2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31"]
            ],
            [],
        ),
    ],
)
def test_em_f_report_period_absence_is_explicit_without_downgrading_complete(
    tmp_path, period_rows, expected_absent
):
    handler, _ = _emf_handler(period_rows)
    acquisition_runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("income_quarter",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        _run_all_rounds(service, run_id)
        report_job = next(
            item
            for item in service.storage.list_jobs(run_id, limit=None)
            if item["purpose"] == "report_period"
        )
        coverage = next(
            item
            for item in service.storage.list_acquisition_coverage(run_id=run_id, limit=None)
            if item.get("job_id") == report_job["job_id"]
        )
        assert coverage["status"] == "complete"
        if expected_absent:
            assert coverage["absent_periods"] == expected_absent
            assert "supplier_period_absent" in coverage["reason_code"]
        else:
            assert "absent_periods" not in coverage
    finally:
        acquisition_runtime.close()
        client.close()


@pytest.mark.parametrize("duplicate", [True, False])
def test_complete_pagination_requires_exact_unique_total(tmp_path, duplicate):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pageNumber"])
        rows = (
            [
                {"SECUCODE": "600519.SH", "TRADE_DATE": "2020-01-02", "DAILY_RANK": 1},
                {"SECUCODE": "600519.SH", "TRADE_DATE": "2020-01-02", "DAILY_RANK": 2},
            ]
            if page == 1
            else [
                {
                    "SECUCODE": "600519.SH",
                    "TRADE_DATE": "2020-01-02",
                    "DAILY_RANK": 2 if duplicate else 3,
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "result": {"data": rows, "count": 3, "pages": 2},
            },
        )

    acquisition_runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("block_trade",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        _run_all_rounds(service, run_id)
        status = service.status(run_id)
        job = service.storage.list_jobs(run_id, limit=None)[0]
        coverage = service.storage.list_acquisition_coverage(run_id=run_id, limit=None)[-1]
        if duplicate:
            assert status["partial"] == 1
            assert coverage["status"] != "complete"
            assert coverage["safe_through"] is None
        else:
            assert status["succeeded"] == 1
            assert coverage["status"] == "complete"
            assert coverage["safe_through"] is not None
    finally:
        acquisition_runtime.close()
        client.close()


class FakeResultSet:
    def __init__(self, rows):
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = ("code", "code_name", "ipoDate", "outDate", "type", "status")
        self._rows = iter(rows)
        self._current = None

    def next(self):
        try:
            self._current = next(self._rows)
        except StopIteration:
            return False
        return True

    def get_row_data(self):
        return self._current


class FakeBaoStock:
    def __init__(self):
        self.login_count = 0
        self.logout_count = 0
        self.calls = []

    def login(self):
        self.login_count += 1
        return SimpleNamespace(error_code="0", error_msg="success")

    def logout(self):
        self.logout_count += 1
        return SimpleNamespace(error_code="0", error_msg="success")

    def query_stock_basic(self, **params):
        self.calls.append(params)
        return FakeResultSet(
            [("sh.600519", "贵州茅台", "20010827", "", "1", "1")]
        )


def test_baostock_sdk_snapshot_keeps_sdk_proof_without_fake_http_200(tmp_path):
    acquisition_runtime, client = _acquisition_runtime(tmp_path)
    sdk = FakeBaoStock()
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime, sdk=sdk)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("baostock_basic",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]

        result = service.run(run_id)

        assert result["status"]["succeeded"] == 1
        assert sdk.calls == [{"code": "sh.600519"}]
        assert sdk.login_count == sdk.logout_count == 1
        job = service.storage.list_jobs(run_id, limit=None)[0]
        page = service.storage.list_pages(job["job_id"])[0]
        snapshot = acquisition_runtime.repository.get_raw_resource_snapshot(
            page["snapshot_id"]
        )
        body = acquisition_runtime.blob_store.read_verified(
            snapshot.archive_relative_path,
            expected_sha256=snapshot.sha256,
            expected_length=snapshot.byte_length,
        )
        proof = json.loads(body)["proof"]
        assert proof["evidence_kind"] == "sdk_result"
        assert proof["sdk_code"] == "0"
        assert proof["result_set_exhausted"] is True
        assert proof["http_status"] is None
    finally:
        acquisition_runtime.close()
        client.close()


def test_failed_http_has_one_attempt_per_round_and_stops_after_two(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503, json={"error": "temporary"})

    acquisition_runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("company_basic",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]

        first = service.run(run_id)
        assert first["status"]["retryable"] == 1
        assert len(acquisition_runtime.repository.list_attempts(run_id=run_id)) == 1

        second = service.resume(run_id)
        assert second["status"]["failed"] == 1
        assert len(acquisition_runtime.repository.list_attempts(run_id=run_id)) == 2
        assert len(calls) == 2

        terminal = service.resume(run_id)
        assert terminal["attempted_job_ids"] == []
        assert len(calls) == 2
    finally:
        acquisition_runtime.close()
        client.close()


def test_incremental_catalog_reuses_old_periods_and_refreshes_two_latest(tmp_path):
    calls=[]
    dates=['2025-12-31','2025-09-30','2025-06-30','2025-03-31']
    def handler(request):
        if request.url.path.endswith('/Index'):
            return httpx.Response(200,headers={'content-type':'text/html'},content=b'<input id="hidctype" value="4">')
        if request.url.path.endswith('zcfzbDateAjaxNew'):
            return httpx.Response(200,json={'code':0,'data':[{'SECURITY_CODE':'600519','REPORT_DATE':d} for d in dates]})
        selected=request.url.params['dates'].split(',');calls.append(selected)
        return httpx.Response(200,json={'code':0,'data':[{'SECUCODE':'600519.SH','REPORT_DATE':d,'ORG_CODE':'1000','TOTAL_ASSETS':1} for d in selected]})
    runtime,client=_acquisition_runtime(tmp_path,handler=handler)
    try:
        service=StructuredDataService.from_runtime(runtime)
        first=service.plan('600519',mode='baseline',company_scope='company-only',datasets=('balance_fields',),as_of=NOW)['run_ids'][0]
        _run_all_rounds(service,first)
        first_incremental_as_of = NOW + timedelta(days=1)
        second=service.plan('600519',mode='incremental',company_scope='company-only',datasets=('balance_fields',),as_of=first_incremental_as_of)['run_ids'][0]
        incremental_jobs = service.storage.list_jobs(second, limit=None)
        catalog_job = next(item for item in incremental_jobs if item["purpose"] == "report_catalog")
        assert datetime.fromisoformat(catalog_job["time_start"]) >= NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        _run_all_rounds(service,second)
        assert calls==[dates,dates[:2]]
        second_incremental_as_of = NOW + timedelta(days=2)
        third=service.plan('600519',mode='incremental',company_scope='company-only',datasets=('balance_fields',),as_of=second_incremental_as_of)['run_ids'][0]
        third_jobs = service.storage.list_jobs(third, limit=None)
        third_catalog = next(item for item in third_jobs if item["purpose"] == "report_catalog")
        assert datetime.fromisoformat(third_catalog["time_start"]).date() >= first_incremental_as_of.date()
        _run_all_rounds(service,third)
        assert calls==[dates,dates[:2],dates[:2]]
        _run_all_rounds(service,second)
        assert len(calls)==3
        assert service.storage.committed_report_periods(service.storage.get_run_context(second).company_id,'balance_fields',second)==set(dates)
    finally:
        runtime.close();client.close()


def test_incremental_cursor_advances_and_event_overlap_stays_bounded(tmp_path):
    from datetime import date

    def handler(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "result": {
                    "data": [
                        {
                            "SECUCODE": "600519.SH",
                            "NOTICE_DATE": "2026-09-01",
                            "ORG_CODE": "1000",
                            "CASE_NAME": "case",
                            "DEFENCE": "defence",
                            "CASE_PROFILE": "profile",
                        }
                    ],
                    "count": 1,
                    "pages": 1,
                },
            },
        )

    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        baseline = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("litigation",),
            as_of=NOW,
        )["run_ids"][0]
        _run_all_rounds(service, baseline)

        first_as_of = NOW + timedelta(days=1)
        first = service.plan(
            "600519",
            mode="incremental",
            company_scope="company-only",
            datasets=("litigation",),
            as_of=first_as_of,
        )["run_ids"][0]
        first_job = service.storage.list_jobs(first, limit=None)[0]
        assert date.fromisoformat(first_job["time_start"][:10]) == NOW.date() - timedelta(days=30)
        _run_all_rounds(service, first)

        second_as_of = NOW + timedelta(days=2)
        second = service.plan(
            "600519",
            mode="incremental",
            company_scope="company-only",
            datasets=("litigation",),
            as_of=second_as_of,
        )["run_ids"][0]
        second_job = service.storage.list_jobs(second, limit=None)[0]
        assert date.fromisoformat(second_job["time_start"][:10]) == first_as_of.date() - timedelta(days=30)
        assert second_job["time_start"] > first_job["time_start"]
    finally:
        runtime.close()
        client.close()


def test_incremental_rejects_a_failed_coverage_window(tmp_path):
    def handler(_request):
        return httpx.Response(503, json={"error": "temporary"})

    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        baseline = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("company_basic",),
            as_of=NOW,
        )["run_ids"][0]
        service.run(baseline)
        service.resume(baseline)
        with pytest.raises(ValueError, match="incremental需要每个适用数据集都有安全coverage"):
            service.plan(
                "600519",
                mode="incremental",
                company_scope="company-only",
                datasets=("company_basic",),
                as_of=NOW + timedelta(days=1),
            )
    finally:
        runtime.close()
        client.close()
