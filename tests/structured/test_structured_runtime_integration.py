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
        assert context.dataset_registry_version == service.runtime.bundle.datasets.version
        job = service.storage.list_jobs(run_id, limit=None)[0]
        source_version = service.runtime.bundle.datasets.source_definition_version
        assert job['source_definition_version'] == (source_version or context.dataset_registry_version)
        assert service.status(run_id)["pending"] == 1
    finally:
        acquisition_runtime.close()
        client.close()

    all_runtime, all_client = _acquisition_runtime(tmp_path / "all")
    try:
        all_service = StructuredDataService.from_runtime(all_runtime)
        initial = all_service.plan("600519", mode="incremental", as_of=NOW)
        assert initial["created"]
        assert initial["dataset_count"] == 22
    finally:
        all_runtime.close()
        all_client.close()


def test_http_page_is_snapshotted_then_projected_under_shared_run(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "datacenter.eastmoney.com"
        assert request.url.params["pageNumber"] == "1"
        assert request.url.params["filter"] == '(SECUCODE="600519.SH")'
        assert '__retrieved_at' not in request.url.params['columns']
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
        assert {item["raw_field_name"] for item in fields} == {
            "SECUCODE",
            "ORG_CODE",
            "ORG_NAME",
        }
        assert all(item['period_key'] is None for item in fields)
        # A later observation of identical upstream content must not create
        # another content version or supply a business period.
        raw = {k: v for k, v in records[0]['raw_row'].items() if k != '__retrieved_at'}
        context = service.storage.get_run_context(run_id)
        dataset = dict(context.frozen_config['datasets'][0])
        dataset['date_fields'] = ['__retrieved_at']  # Legacy frozen contract.
        later = datetime.fromisoformat(records[0]['observed_at']) + timedelta(days=1)
        repeated_records, repeated_fields = service.runtime._project_rows(
            context, job, dataset,
            SimpleNamespace(rows=(raw,), page_number=1, response_sha256=pages[0]['content_hash']),
            snapshot_id=pages[0]['snapshot_id'], observed_at=later,
        )
        assert repeated_records[0]['version_hash'] == records[0]['version_hash']
        assert repeated_records[0]['raw_row']['__retrieved_at'] == later.isoformat()
        assert all(item['period_key'] is None for item in repeated_fields)
        assert '__retrieved_at' not in {item['raw_field_name'] for item in repeated_fields}
        assert coverage[0]["status"] == "complete"
        assert snapshot.physical_query_plan_item_id == job["plan_item_id"]
    finally:
        acquisition_runtime.close()
        client.close()


def test_reconcile_targets_only_failed_finalized_parent_coverage(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) <= 2:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "result": {
                    "data": [{"SECUCODE": "600519.SH", "ORG_CODE": "1000"}],
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
            datasets=("company_basic",),
            as_of=NOW,
        )["run_ids"][0]
        service.run(baseline)
        service.resume(baseline)
        assert service.status(baseline)["failed"] == 1
        target = service.plan(
            "600519",
            mode="reconcile",
            parent_run_id=baseline,
            company_scope="company-only",
            datasets=("company_basic",),
            as_of=NOW + timedelta(days=1),
        )
        assert target["mode"] == "reconcile"
        assert target["created"] is True
        reconcile_id = target["run_ids"][0]
        run = runtime.repository.get_run(reconcile_id)
        assert run.mode.value == "reconcile"
        assert run.parent_run_id == baseline
        assert run.reconcile_target["selection"] == "latest_unsafe_structured_coverage"
        assert target["runs"][0]["job_count"] == 1
        assert len(calls) == 2
        _run_all_rounds(service, reconcile_id)
        assert service.status(reconcile_id)["succeeded"] == 1
        coverage = service.storage.list_acquisition_coverage(
            run_id=reconcile_id, limit=None
        )
        assert coverage[0]["status"] == "complete"
        assert len(calls) == 3
    finally:
        runtime.close()
        client.close()


def test_reconcile_rejects_successful_parent_coverage(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "result": {
                    "data": [{"SECUCODE": "600519.SH", "ORG_CODE": "1000"}],
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
            datasets=("company_basic",),
            as_of=NOW,
        )["run_ids"][0]
        _run_all_rounds(service, baseline)
        with pytest.raises(ValueError, match="reconcile没有失败、空响应或不安全coverage目标"):
            service.plan(
                "600519",
                mode="reconcile",
                parent_run_id=baseline,
                company_scope="company-only",
                datasets=("company_basic",),
                as_of=NOW + timedelta(days=1),
            )
    finally:
        runtime.close()
        client.close()


def test_reconcile_from_latest_skips_unfinalized_structured_run(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 0,
                "result": {
                    "data": [{"SECUCODE": "600519.SH", "ORG_CODE": "1000"}],
                    "count": 1,
                    "pages": 1,
                },
            },
        )

    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        finalized = service.plan(
            "600519", mode="baseline", datasets=("company_basic",), as_of=NOW
        )["run_ids"][0]
        _run_all_rounds(service, finalized)
        service.plan(
            "600519",
            mode="baseline",
            datasets=("company_basic",),
            as_of=NOW + timedelta(days=1),
        )
        with pytest.raises(ValueError, match="reconcile没有失败、空响应或不安全coverage目标"):
            service.plan(
                "600519",
                mode="reconcile",
                from_latest=True,
                datasets=("company_basic",),
                as_of=NOW + timedelta(days=2),
            )
    finally:
        runtime.close()
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
        assert coverage["query_complete"] is True
        assert coverage["safe_through"] is not None
        service.plan("600519", mode="incremental", datasets=("income_quarter",),
                     as_of=NOW + timedelta(days=1))
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
        original_records = [
            record
            for job in service.storage.list_jobs(second, limit=None)
            for record in service.storage.list_records(job_id=job["job_id"], limit=None)
        ]
        original_pages = [
            page
            for job in service.storage.list_jobs(second, limit=None)
            for page in service.storage.list_pages(job["job_id"])
        ]
        repeated = service.plan(
            '600519', mode='incremental', company_scope='company-only',
            datasets=('balance_fields',), as_of=first_incremental_as_of,
        )
        assert repeated['run_ids'] == [second]
        assert repeated['created'] is False
        assert service.run(second)['attempted_job_ids'] == []
        assert [
            record
            for job in service.storage.list_jobs(second, limit=None)
            for record in service.storage.list_records(job_id=job["job_id"], limit=None)
        ] == original_records
        assert [
            page
            for job in service.storage.list_jobs(second, limit=None)
            for page in service.storage.list_pages(job["job_id"])
        ] == original_pages
        assert calls == [dates, dates[:2]]
        second_incremental_as_of = NOW + timedelta(days=2)
        third=service.plan('600519',mode='incremental',company_scope='company-only',datasets=('balance_fields',),as_of=second_incremental_as_of)['run_ids'][0]
        third_jobs = service.storage.list_jobs(third, limit=None)
        third_catalog = next(item for item in third_jobs if item["purpose"] == "report_catalog")
        assert datetime.fromisoformat(third_catalog["time_start"]).date() >= first_incremental_as_of.date()
        _run_all_rounds(service,third)
        assert calls==[dates,dates[:2],dates[:2]]
        # Later successful runs must not change the identity of this request.
        repeated_after_later_run = service.plan(
            '600519', mode='incremental', company_scope='company-only',
            datasets=('balance_fields',), as_of=first_incremental_as_of,
        )
        assert repeated_after_later_run['run_ids'] == [second]
        assert repeated_after_later_run['created'] is False
        _run_all_rounds(service,second)
        assert len(calls)==3
        assert service.storage.committed_report_periods(service.storage.get_run_context(second).company_id,'balance_fields',second)==set(dates)
    finally:
        runtime.close();client.close()


@pytest.mark.parametrize(
    "changed",
    [
        {"mode": "due"},
        {"report_periods": ("2025-12-31",)},
        {"company_scope": "peer-set"},
    ],
)
def test_repeated_plan_never_aliases_a_different_frozen_request(tmp_path, changed):
    from analysis.structured.planner import StructuredPlanningError

    runtime, client = _acquisition_runtime(tmp_path)
    try:
        service = StructuredDataService.from_runtime(runtime)
        identity = service.resolver.resolve("600519", as_of=NOW.date()).identity
        request = dict(
            mode="baseline", company_scope="company-only",
            dataset_ids=("balance_fields",), as_of=NOW,
        )
        first = service.runtime.plan((identity,), **request)
        jobs = service.storage.list_jobs(first.run_ids[0], limit=None)
        with pytest.raises(StructuredPlanningError, match="overlap|different frozen request"):
            service.runtime.plan((identity,), **(request | changed))
        assert service.storage.list_jobs(first.run_ids[0], limit=None) == jobs
        assert runtime.repository.list_attempts(run_id=first.run_ids[0]) == []
    finally:
        runtime.close()
        client.close()


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


def test_incremental_schedules_failed_window_without_advancing_it(tmp_path):
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
        incremental = service.plan(
                "600519",
                mode="incremental",
                company_scope="company-only",
                datasets=("company_basic",),
                as_of=NOW + timedelta(days=1),
            )
        new_job = service.storage.list_jobs(incremental["run_ids"][0], limit=None)[0]
        original = service.storage.list_jobs(baseline, limit=None)[0]
        assert new_job["reconciles_job_id"] == original["job_id"]
        assert (new_job["time_start"], new_job["time_end"]) == (original["time_start"], original["time_end"])
    finally:
        runtime.close()
        client.close()


def test_default_deferral_is_company_scoped_and_explicit_selection_is_distinct(tmp_path):
    from analysis.structured.scope import load_scope

    runtime, client = _acquisition_runtime(tmp_path)
    try:
        service = StructuredDataService.from_runtime(runtime)
        planned = service.plan('600519', as_of=NOW, company_scope='company-with-peers')
        deferred = set(load_scope()['company_acquisition_deferrals']['600519.SH']['dataset_ids'])
        assert len(deferred) == 9
        assert set(planned['deferred_datasets_by_ticker']) == {'600519.SH'}
        for run in planned['runs']:
            jobs = service.storage.list_jobs(run['run_id'], limit=None)
            actual = {job['dataset_id'] for job in jobs}
            if run['ticker'] == '600519.SH':
                moutai = run
                assert len(actual) == 22
                assert not actual & deferred
                assert set(service.status(run['run_id'])['acquisition_deferral']['dataset_ids']) == deferred
                assert runtime.repository.get_run(run['run_id']).request_scope == 'ad_hoc'
            else:
                assert deferred <= actual
                assert run['acquisition_deferral'] is None
        repeated = service.plan('600519', as_of=NOW, company_scope='company-with-peers')
        assert not repeated['created']
        assert repeated['run_ids'] == planned['run_ids']
        explicit = service.plan('600519', as_of=NOW, datasets=moutai['dataset_ids'])
        assert explicit['run_ids'][0] != moutai['run_id']
        assert explicit['deferred_datasets_by_ticker'] == {}
        later_supplement = service.plan('600519', as_of=NOW, datasets=['goodwill'])
        assert later_supplement['dataset_ids'] == ['goodwill']
        assert later_supplement['deferred_datasets_by_ticker'] == {}
    finally:
        runtime.close()
        client.close()


def test_deferred_empty_allows_incremental_new_rows_and_reopen_resume(tmp_path, monkeypatch):
    from copy import deepcopy
    from analysis.structured import scope
    from analysis.structured.storage import canonical_sha256

    # Exercise real planning, snapshots and coverage with controlled upstream
    # changes. The historical empty result must survive the scope adjustment.
    current_scope = deepcopy(scope.load_scope())
    for dataset_id, rule in current_scope['datasets'].items():
        if rule['selection'] == 'required' and dataset_id not in {'company_basic', 'controller', 'goodwill'}:
            rule['selection'] = 'conditional'
    current_scope['content_sha256'] = canonical_sha256({k: v for k, v in current_scope.items() if k != 'content_sha256'})
    monkeypatch.setattr(scope, 'load_scope', lambda: current_scope)
    phase = 0
    calls = []

    def handler(request):
        name = request.url.params['reportName']
        calls.append(name)
        if name == 'RPT_GOODWILL_STOCKDETAILS':
            return httpx.Response(200, json={'success': False, 'code': 9201, 'message': '返回数据为空', 'result': None})
        rows = [{'SECUCODE': '600519.SH', 'ORG_CODE': '1000', 'ORG_NAME': 'company'}]
        assert name in {'RPT_F10_ORG_BASICINFO', 'RPT_F10_EH_RELATION'}
        if name == 'RPT_F10_EH_RELATION':
            rows = [{'SECUCODE': '600519.SH', 'HOLDER_NAME': holder, 'HOLD_RATIO': 10} for holder in (['A'] if phase == 0 else ['A', 'B'])]
        return httpx.Response(200, json={'success': True, 'code': 0, 'result': {'data': rows, 'pages': 1, 'count': len(rows)}})

    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    service = StructuredDataService.from_runtime(runtime)
    try:
        base = service.plan('600519', as_of=NOW, datasets=['company_basic', 'controller', 'goodwill'])['run_ids'][0]
        _run_all_rounds(service, base)
        assert service.status(base)['no_data'] == 1
        original_empty = service.storage.list_acquisition_coverage(company_id=service.storage.get_run_context(base).company_id, dataset_id='goodwill', limit=None)
        phase = 1
        before = len(calls)
        cutoff = NOW + timedelta(days=1)
        plan = service.plan('600519', mode='incremental', as_of=cutoff)
        run_id = plan['run_ids'][0]
        assert set(plan['dataset_ids']) == {'company_basic', 'controller'}
        first_round = service.run(run_id)
        assert first_round['status']['pending'] == 1
        completed_jobs = set(first_round['attempted_job_ids'])
    finally:
        runtime.close()
        client.close()

    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        resumed = service.resume(run_id)
        assert not completed_jobs & set(resumed['attempted_job_ids'])
        assert service.status(run_id)['succeeded'] == 2
        controller = next(job for job in service.storage.list_jobs(run_id, limit=None) if job['dataset_id'] == 'controller')
        records = service.storage.list_records(job_id=controller['job_id'], limit=None)
        assert {row['raw_row']['HOLDER_NAME'] for row in records} == {'A', 'B'}
        assert 'RPT_GOODWILL_STOCKDETAILS' not in calls[before:]
        assert service.storage.list_acquisition_coverage(company_id=service.storage.get_run_context(base).company_id, dataset_id='goodwill', limit=None) == original_empty
        call_count = len(calls)
        repeated = service.plan('600519', mode='incremental', as_of=cutoff)
        assert not repeated['created']
        assert service.run(run_id)['attempted_job_ids'] == []
        assert len(calls) == call_count
        explicit = service.plan('600519', mode='incremental', as_of=cutoff, datasets=['goodwill'])
        _run_all_rounds(service, explicit['run_ids'][0])
        assert service.status(explicit['run_ids'][0])['completed_empty_jobs'] == 1
    finally:
        runtime.close()
        client.close()


def _empty_response():
    return httpx.Response(200, json={
        "success": False, "code": 9201, "message": "返回数据为空", "result": None,
    })


def _litigation_response():
    return httpx.Response(200, json={
        "success": True, "code": 0,
        "result": {"data": [{
            "SECUCODE": "600519.SH", "NOTICE_DATE": "2026-09-10",
            "ORG_CODE": "1000", "CASE_NAME": "new case",
            "DEFENCE": "defence", "CASE_PROFILE": "profile",
        }], "count": 1, "pages": 1},
    })


@pytest.mark.parametrize("interruption", ["coverage", "outcome"])
def test_resume_saved_terminal_finishes_without_requesting_an_extra_page(tmp_path, monkeypatch, interruption):
    calls = []
    def handler(request):
        calls.append(int(request.url.params["pageNumber"]))
        return _litigation_response()
    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        run = service.plan("600519", datasets=["litigation"], as_of=NOW)["run_ids"][0]
        name = "_append_acquisition_coverage" if interruption == "coverage" else "_append_attempt_outcome"
        original = getattr(service.runtime, name)
        def crash(*args, **kwargs):
            raise KeyboardInterrupt("after saving terminal page")
        monkeypatch.setattr(service.runtime, name, crash)
        with pytest.raises(KeyboardInterrupt):
            service.run(run)
        assert calls == [1]
        assert service.status(run)["summary"]["unfinished"][0]["resume_action"] == "finish_saved_terminal"
        monkeypatch.setattr(service.runtime, name, original)
        service.resume(run)
        assert calls == [1]
        assert service.status(run)["succeeded"] == 1
        bundles = list(service.storage.iter_committed_record_bundles(run))
        assert len(bundles) == 1
        assert bundles[0][0]["_committed_attempt_outcome"] == "success"
    finally:
        runtime.close()
        client.close()


@pytest.mark.parametrize("recovery_path", ["resume", "incremental"])
def test_resume_retries_saved_duplicate_page_and_preserves_original_evidence(tmp_path, recovery_path):
    calls = []
    def handler(request):
        number = int(request.url.params["pageNumber"])
        calls.append(number)
        ranks = [1, 2] if number == 1 else [2] if calls.count(2) == 1 else [3]
        return httpx.Response(200, json={"success": True, "code": 0, "result": {
            "data": [{"SECUCODE": "600519.SH", "TRADE_DATE": "2020-01-02", "DAILY_RANK": rank} for rank in ranks],
            "count": 3, "pages": 2,
        }})
    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        run = service.plan("600519", datasets=["block_trade"], as_of=NOW)["run_ids"][0]
        service.run(run)
        assert calls == [1, 2]
        assert service.status(run)["partial"] == 1
        assert service.status(run)["summary"]["unfinished"][0]["resume_page"] == 2
        job = service.storage.list_jobs(run, limit=None)[0]
        old_pages = service.storage.list_pages(job["job_id"], include_history=True)
        original_job = job
        if recovery_path == "incremental":
            run = service.plan("600519", mode="incremental", datasets=["block_trade"],
                               as_of=NOW + timedelta(days=1))["run_ids"][0]
            job = service.storage.list_jobs(run, limit=None)[0]
        service.resume(run)
        assert calls == [1, 2, 2]
        assert service.status(run)["succeeded"] == 1
        history = service.storage.list_pages(job["job_id"], include_history=True)
        original_history = service.storage.list_pages(original_job["job_id"], include_history=True)
        assert all(page in original_history for page in old_pages)
        assert len(history) == (3 if recovery_path == "resume" else 2)
        assert len(service.storage.list_pages(job["job_id"])) == 2
        records = service.storage.list_records(job_id=job["job_id"], limit=None)
        assert sorted(row["raw_row"]["DAILY_RANK"] for row in records) == [1, 2, 3]
        bundles = list(service.storage.iter_committed_record_bundles(run))
        assert len(bundles) == 3
        assert all(row[0]["_committed_attempt_outcome"] == "success" for row in bundles)
    finally:
        runtime.close()
        client.close()


def test_failed_dataset_does_not_stop_healthy_incremental_and_reports_recovery(tmp_path):
    def handler(request):
        if request.url.params.get("reportName") == "RPT_F10_ORG_BASICINFO":
            return httpx.Response(200, json={"success": True, "code": 0,
                "result": {"data": [{"SECUCODE": "600519.SH", "ORG_CODE": "1000"}], "count": 1, "pages": 1}})
        return httpx.Response(503, json={"error": "temporarily unavailable"})
    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        args = {"datasets": ["company_basic", "litigation"]}
        baseline = service.plan("600519", as_of=NOW, **args)["run_ids"][0]
        _run_all_rounds(service, baseline)
        assert service.status(baseline)["succeeded"] == 1
        assert service.status(baseline)["failed"] == 1
        incremental = service.plan("600519", mode="incremental", as_of=NOW + timedelta(days=1), **args)["run_ids"][0]
        _run_all_rounds(service, incremental)
        status = service.status(incremental)
        assert status["succeeded"] == 1
        assert status["failed"] == 1
        assert status["summary"]["runnable_finished"] is True
        unfinished = status["summary"]["unfinished"]
        assert len(unfinished) == 1
        assert unfinished[0]["dataset_id"] == "litigation"
        assert unfinished[0]["resume_page"] == 1
        old = next(job for job in service.storage.list_jobs(baseline, limit=None) if job["dataset_id"] == "litigation")
        assert unfinished[0]["time_end"] == old["time_end"]
    finally:
        runtime.close()
        client.close()


def test_only_tasks_missing_company_type_are_deferred(tmp_path):
    runtime, client = _acquisition_runtime(tmp_path, handler=lambda request:
        httpx.Response(503, json={"error": "unavailable"}) if request.url.path.endswith("/Index")
        else _empty_response())
    try:
        service = StructuredDataService.from_runtime(runtime)
        run = service.plan("600519", datasets=["income_quarter", "litigation"], as_of=NOW)["run_ids"][0]
        _run_all_rounds(service, run)
        status = service.status(run)
        assert status["failed"] == 1
        assert status["blocked"] == 1
        assert status["completed_empty_jobs"] == 1
        waiting = next(row for row in status["summary"]["unfinished"] if row["state"] == "blocked")
        assert waiting["missing_prerequisite_job_ids"]
        assert waiting["resume_action"] == "wait_for_prerequisite"
        assert status["summary"]["runnable_finished"] is True
    finally:
        runtime.close()
        client.close()


@pytest.mark.parametrize("baseline_has_data", [False, True])
def test_complete_empty_query_allows_future_network_updates_and_keeps_records(tmp_path, baseline_has_data):
    phase = "baseline"
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if phase == "new_data" or (phase == "baseline" and baseline_has_data):
            return _litigation_response()
        return _empty_response()

    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        args = {"datasets": ["litigation"], "company_scope": "company-only"}
        def records(run_id):
            return [record for job in service.storage.list_jobs(run_id, limit=None)
                    for record in service.storage.list_records(job_id=job["job_id"], limit=None)]

        baseline = service.plan("600519", as_of=NOW, **args)["run_ids"][0]
        _run_all_rounds(service, baseline)
        old_records = records(baseline)
        phase = "empty"
        cutoff = NOW + timedelta(days=1)
        empty_run = service.plan("600519", mode="incremental", as_of=cutoff, **args)["run_ids"][0]
        _run_all_rounds(service, empty_run)
        assert service.status(empty_run)["no_data"] == 1
        assert service.status(empty_run)["completed_empty_jobs"] == 1
        coverage = service.storage.list_acquisition_coverage(run_id=empty_run, limit=None)[-1]
        assert coverage["status"] == "no_data"
        assert coverage["query_complete"] is True
        assert coverage["returned_row_count"] == 0
        assert coverage["safe_through"] == service.storage.list_jobs(empty_run, limit=None)[0]["time_end"]
        assert coverage["pagination_audit"]["complete"] is True
        assert records(baseline) == old_records
        assert records(empty_run) == []
        count = len(calls)
        assert not service.plan("600519", mode="incremental", as_of=cutoff, **args)["created"]
        assert service.run(empty_run)["attempted_job_ids"] == []
        assert len(calls) == count
        phase = "new_data"
        future = service.plan("600519", mode="incremental", as_of=NOW + timedelta(days=2), **args)["run_ids"][0]
        _run_all_rounds(service, future)
        assert len(calls) > count
        assert service.status(future)["succeeded"] == 1
        assert len(records(future)) == 1
    finally:
        runtime.close()
        client.close()


@pytest.mark.parametrize("failure", ["timeout", "http_error", "business_error", "missing_page"])
def test_incomplete_incremental_keeps_recovery_window_after_empty_baseline(tmp_path, failure):
    phase = "baseline"

    def handler(request):
        if phase == "baseline":
            return _empty_response()
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "http_error":
            return httpx.Response(503, json={"error": "temporary"})
        if failure == "business_error":
            return httpx.Response(200, json={"success": False, "code": 9501, "result": None})
        if request.url.params["pageNumber"] == "1":
            payload = _litigation_response().json()
            payload["result"].update(count=2, pages=2)
            return httpx.Response(200, json=payload)
        return _empty_response()

    runtime, client = _acquisition_runtime(tmp_path, handler=handler)
    try:
        service = StructuredDataService.from_runtime(runtime)
        args = {"datasets": ["litigation"], "company_scope": "company-only"}
        baseline = service.plan("600519", as_of=NOW, **args)["run_ids"][0]
        _run_all_rounds(service, baseline)
        phase = "failure"
        failed = service.plan("600519", mode="incremental", as_of=NOW + timedelta(days=1), **args)["run_ids"][0]
        _run_all_rounds(service, failed)
        coverage = service.storage.list_acquisition_coverage(run_id=failed, limit=None)[-1]
        assert coverage["safe_through"] is None
        assert coverage["query_complete"] is False
        assert service.status(failed)["completed_empty_jobs"] == 0
        recovery = service.plan("600519", mode="incremental",
                                    as_of=NOW + timedelta(days=2), **args)["run_ids"][0]
        old_job = service.storage.list_jobs(failed, limit=None)[0]
        new_job = service.storage.list_jobs(recovery, limit=None)[0]
        assert (new_job["time_start"], new_job["time_end"]) == (old_job["time_start"], old_job["time_end"])
        phase = "baseline"
        _run_all_rounds(service, recovery)
        assert service.status(recovery)["completed_empty_jobs"] == 1
        service.plan("600519", mode="incremental", as_of=NOW + timedelta(days=3), **args)
    finally:
        runtime.close()
        client.close()


def test_reconcile_old_empty_coverage_appends_proof_and_unblocks_future_incremental(tmp_path, monkeypatch):
    runtime, client = _acquisition_runtime(tmp_path, handler=lambda _: _empty_response())
    try:
        service = StructuredDataService.from_runtime(runtime)
        args = {"datasets": ["litigation"], "company_scope": "company-only"}
        # Load a pre-rule empty result through the old persistence contract.
        original_append = service.storage.append_acquisition_coverage

        def legacy_append(value):
            legacy = {k: v for k, v in value.items() if k not in {"query_complete", "completion_rule"}}
            legacy["safe_through"] = None
            original_append(legacy)

        monkeypatch.setattr(service.storage, "append_acquisition_coverage", legacy_append)
        baseline = service.plan("600519", as_of=NOW, **args)["run_ids"][0]
        _run_all_rounds(service, baseline)
        original = service.storage.list_acquisition_coverage(run_id=baseline, limit=None)
        monkeypatch.setattr(service.storage, "append_acquisition_coverage", original_append)
        reconcile = service.plan("600519", mode="reconcile", parent_run_id=baseline,
                                 as_of=NOW + timedelta(days=1), **args)["run_ids"][0]
        _run_all_rounds(service, reconcile)
        assert service.status(reconcile)["completed_empty_jobs"] == 1
        assert service.storage.list_acquisition_coverage(run_id=baseline, limit=None) == original
        recovered = service.storage.list_acquisition_coverage(run_id=reconcile, limit=None)[-1]
        assert recovered["reconciles_scope_key"] == original[0]["scope_key"]
        future = service.plan("600519", mode="incremental", as_of=NOW + timedelta(days=2), **args)
        _run_all_rounds(service, future["run_ids"][0])
        assert service.status(future["run_ids"][0])["completed_empty_jobs"] == 1
        with pytest.raises(ValueError, match="reconcile没有"):
            service.plan("600519", mode="reconcile", parent_run_id=reconcile,
                         as_of=NOW + timedelta(days=3), **args)
    finally:
        runtime.close()
        client.close()
