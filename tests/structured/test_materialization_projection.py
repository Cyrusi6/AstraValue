import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import duckdb
import httpx
import pytest

from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.structured.service import StructuredDataService
from analysis.structured.storage import StructuredStorage
from analysis.timeseries import TimeSeriesStore, TimeSeriesStorageError
from analysis.storage import StorageError


class NoWaitGate:
    @contextmanager
    def hold(self, *_args, **_kwargs):
        yield SimpleNamespace()


def test_bound_numeric_dimension_event_projection_and_lineage(tmp_path):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        report = request.url.params.get("reportName") or request.url.params.get("type")
        data = {
            "RPT_F10_EH_FREEHOLDERS": [
                {"SECUCODE": "600519.SH", "END_DATE": "2025-12-31", "HOLDER_NAME": name,
                 "FREE_HOLDNUM_RATIO": ratio} for name, ratio in [("甲", 10), ("乙", 20)]],
            "RPT_SHAREBONUS_DET": [
                {"SECUCODE": "600519.SH", "SECURITY_CODE": "600519", "REPORT_DATE": "2025-12-31",
                 "ORG_CODE": "ORG", "NOTICE_DATE": "2026-01-01", "PRETAX_BONUS_RMB": 20,
                 "ASSIGN_PROGRESS": "预案"}],
            "RPT_VALUEANALYSIS_DET": [
                {"SECUCODE": "600519.SH", "TRADE_DATE": "2026-01-01", "CLOSE_PRICE": 1500,
                 "TOTAL_MARKET_CAP": 100000, "TOTAL_SHARES": 100}],
        }[report]
        return httpx.Response(200, json={"success": True, "code": 0,
            "result": {"data": data, "count": len(data), "pages": 1}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with AcquisitionRuntime.create(tmp_path / "bound.db", tmp_path / "data",
            http_client=client, sleeper=lambda _: None) as runtime:
        runtime.source_gate = NoWaitGate()
        service = StructuredDataService.from_runtime(runtime)
        planned = service.plan("600519", mode="baseline", company_scope="company-only",
            datasets=("float_holders_history", "dividend", "market_cap"),
            as_of=datetime(2026, 9, 1, tzinfo=timezone.utc))
        run_id = planned["run_ids"][0]
        result = service.runtime.execute(run_id, max_jobs_per_round=3)
        assert result["status"]["succeeded"] == 3, result
        before = len(calls)
        before_rows = {t: _count(runtime.db_path, t) for t in
            ["structured_records", "structured_record_fields", "raw_resource_snapshots", "acquisition_attempts"]}
        first = service.materialize(run_id)
        second = service.materialize(run_id)
        assert first == second
        assert len(calls) == before
        assert {t: _count(runtime.db_path, t) for t in before_rows} == before_rows
        assert first["fact_count"] == 3
        assert first["dimensional_fact_count"] == 2
        assert first["event_count"] == 1
        assert sorted(f["value"] for f in first["dimensional_facts"]) == [0.1, 0.2]
        report = runtime.report_storage
        manifest = report.get_materialization(first["materialization_hash"])
        assert manifest["fact_ids"] == [f["fact_id"] for f in first["facts"]]
        for f in first["facts"]:
            lineage = report.fact_lineage(f["fact_id"])
            assert lineage["structured_evidence"]["field"]["value"] == f["structured_admission"]["original_value"]
            assert lineage["structured_evidence"]["snapshot"]["sha256"] == f["structured_admission"]["snapshot_sha256"]
        for f in first["dimensional_facts"]:
            lineage = report.dimensional_fact_lineage(f["dimensional_fact_id"])
            assert lineage["structured_evidence"]["record"]["raw_row"]["HOLDER_NAME"] == f["dimension_name"]
        assert report.event_lineage(first["events"][0]["event_id"])["structured_evidence"]["field"]["nature"] == "announced_plan"
        connection = duckdb.connect()
        for name, expected in [("facts", first["facts"]), ("research", first["dimensional_facts"] + first["events"])]:
            path = first["projection"][name]["path"]
            rows = connection.execute("SELECT payload FROM read_parquet(?)", [path]).fetchall()
            assert {json.dumps(json.loads(r[0]), sort_keys=True) for r in rows} == {
                json.dumps(r, sort_keys=True, ensure_ascii=True) for r in expected}
        connection.close()
        assert _count(runtime.db_path, "facts") == 3
        assert _count(runtime.db_path, "structured_fact_materializations") == 1
        # A summary does not serialize the full field payload again.
        summary = service.materialize(run_id, include_records=False)
        assert "facts" not in summary and summary["persisted"]
        dry = service.materialize(run_id, persist=False)
        assert not dry["persisted"] and dry["projection"] is None
    client.close()


def _count(path, table):
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_bulk_reader_respects_low_sqlite_parameter_limit_and_global_limit():
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE structured_record_fields(record_version_id TEXT, field_path TEXT, field_value_id TEXT, payload TEXT)")
        rows = [(f"record-{i:05}", "$.v", f"field-{i}", json.dumps({"index": i})) for i in range(2200)]
        connection.executemany("INSERT INTO structured_record_fields VALUES (?,?,?,?)", rows)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
        ids = [r[0] for r in reversed(rows)]
        all_rows = StructuredStorage._read_payload_chunks(connection, "structured_record_fields",
            "record_version_id", ids, "record_version_id,field_path,field_value_id")
        limited = StructuredStorage._read_payload_chunks(connection, "structured_record_fields",
            "record_version_id", ids, "record_version_id,field_path,field_value_id", 100)
        assert [r["index"] for r in all_rows] == list(range(2200))
        assert limited == all_rows[:100]
        assert StructuredStorage._read_payload_chunks(connection, "structured_record_fields",
            "record_version_id", ids, "record_version_id", 0) == []


def test_timeseries_rejects_changed_payload_instead_of_silent_ignore(tmp_path):
    from analysis.models import FactRecord
    store = TimeSeriesStore(tmp_path / "values.duckdb", tmp_path / "parquet")
    fact = FactRecord(fact_id="stable", ticker="600519", metric_id="cash", value=1,
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc))
    store.append_facts([fact])
    store.append_facts([fact])
    with pytest.raises(TimeSeriesStorageError, match="不可覆盖"):
        store.append_facts([fact.model_copy(update={"value": 2})])
    assert store.query("600519")[0]["value"] == 1


def test_stream_reader_crosses_batches_without_including_other_runs_or_orphan_pages():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE structured_jobs(job_id TEXT,run_id TEXT,storage_namespace_id TEXT);
        CREATE TABLE structured_records(record_version_id TEXT,job_id TEXT,page_id TEXT,snapshot_id TEXT,
            row_key TEXT,available_at TEXT,payload TEXT);
        CREATE TABLE structured_pages(page_id TEXT,job_id TEXT,snapshot_id TEXT,attempt_id TEXT);
        CREATE TABLE acquisition_attempt_events(attempt_id TEXT,event_type TEXT,outcome TEXT,occurred_at TEXT,event_id TEXT);
        CREATE TABLE structured_record_fields(record_version_id TEXT,field_path TEXT,field_value_id TEXT,payload TEXT);
        CREATE TABLE structured_acquisition_coverage(job_id TEXT,payload TEXT);
    """)
    connection.execute("INSERT INTO structured_jobs VALUES ('job','run','ns'),('foreign','other','ns')")
    connection.execute("INSERT INTO structured_pages VALUES ('page','job','snapshot','attempt')")
    connection.execute("INSERT INTO acquisition_attempt_events VALUES ('attempt','outcome_terminal','success','2025','event')")
    for i in range(1025):
        record_id = f"r{i:05}"
        record = {"record_version_id": record_id, "ordinal": i}
        connection.execute("INSERT INTO structured_records VALUES (?,?,?,?,?,?,?)",
            (record_id, "job", "page", "snapshot", record_id, "2025", json.dumps(record)))
        connection.execute("INSERT INTO structured_record_fields VALUES (?,?,?,?)",
            (record_id, "$.v", f"f{i}", json.dumps({"record_version_id": record_id, "value": i})))
    connection.execute("INSERT INTO structured_records VALUES ('orphan','job','missing','snapshot','x','2025','{}')")
    connection.execute("INSERT INTO structured_records VALUES ('foreign','foreign','page','snapshot','x','2025','{}')")
    connection.commit()
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
    @contextmanager
    def connect(**_):
        yield connection
    storage = object.__new__(StructuredStorage)
    storage._connect = connect
    storage.storage_namespace_id = "ns"
    statements = []
    connection.set_trace_callback(statements.append)
    bundles = list(storage.iter_committed_record_bundles("run", batch_size=256))
    assert [r["ordinal"] for r, _ in bundles] == list(range(1025))
    assert all(r["_committed_attempt_outcome"] == "success" for r, _ in bundles)
    assert [fields[0]["value"] for _, fields in bundles] == list(range(1025))
    assert len([s for s in statements if s.startswith("SELECT")]) < 30
    connection.close()


def test_cli_materialize_summary_uses_only_the_requested_bound_service(monkeypatch, capsys):
    from analysis import cli
    calls = []
    class Service:
        def materialize(self, run_id, **kwargs):
            calls.append((run_id, kwargs))
            return {"run_id": run_id, "performed_network_io": False}
    monkeypatch.setattr(cli, "_create_structured_service", lambda db, root: Service())
    assert cli.main(["structured", "materialize", "run", "--db", "bound.db",
        "--data-root", "data", "--as-of", "2026-01-01T00:00:00+00:00",
        "--strict-historical", "--no-persist", "--summary", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["performed_network_io"] is False
    assert calls[0][1]["persist"] is False
    assert calls[0][1]["include_records"] is False
    assert calls[0][1]["strict_historical"] is True


def test_materialization_manifest_api_uses_existing_report_storage(service, monkeypatch):
    from fastapi.testclient import TestClient
    from analysis.api import create_app
    monkeypatch.setattr(service.storage, "get_materialization", lambda key: {
        "materialization_hash": key, "selected_fact_ids": ["fact-1"], "field_gaps": []})
    client = TestClient(create_app(service))
    response = client.get("/api/structured/materializations/abc")
    assert response.status_code == 200
    assert response.json()["selected_fact_ids"] == ["fact-1"]
