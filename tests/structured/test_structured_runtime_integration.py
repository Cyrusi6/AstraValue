from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.models import SyncRequest
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
        result = all_service.sync(
            "600519",
            SyncRequest(source_strategy="structured-first-v1", datasets=[], as_of=NOW),
        )
        assert result.acquisition_status == "planned"
        assert result.default_consume_eligible is False
        assert result.structured_dataset_coverage["dataset_count"] == 55
        assert len(result.structured_dataset_coverage["dataset_ids"]) == 55
    finally:
        all_runtime.close()
        all_client.close()


def test_http_page_is_snapshotted_then_projected_under_shared_run(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "datacenter.eastmoney.com"
        assert request.url.params["pageNumber"] == "1"
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
