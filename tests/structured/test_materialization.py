from datetime import datetime, timezone
from types import SimpleNamespace

from analysis.structured.materialization import StructuredFactMaterializer


class _Storage:
    def get_run_context(self, run_id):
        return SimpleNamespace(
            run_id=run_id,
            ticker="600519",
            field_registry_version="1.1.0",
        )

    def list_jobs(self, run_id, *, limit=None, offset=0):
        return [{"job_id": "job-1", "run_id": run_id, "dataset_id": "income_quarter",
                 "source_definition_id": "structured-eastmoney",
                 "source_definition_version": "1.0.0"}]

    def list_records(self, *, job_id=None, limit=None, offset=0):
        return [{"record_version_id": "record-1", "job_id": job_id,
                 "snapshot_id": "snapshot-1", "row_key": "row-1",
                 "available_at": "2026-09-10T00:00:00+00:00",
                 "observed_at": "2026-09-10T00:00:00+00:00",
                 "raw_row": {"REPORT_DATE": "2026-06-30 00:00:00"}}]

    def list_records_for_jobs(self, job_ids, *, limit=None):
        return self.list_records(job_id="job-1", limit=limit)

    def list_record_fields(self, *, record_version_id=None, limit=None, offset=0,
                           standard_field_id=None):
        return [{"field_value_id": "field-1", "record_version_id": record_version_id,
                 "dataset_id": "income_quarter", "raw_field_name": "TOTAL_OPERATE_INCOME",
                 "field_path": "$.TOTAL_OPERATE_INCOME", "standard_field_id": "total_operating_income",
                 "definition_version": "1.1.0", "nature": "observed", "quality": "passed",
                 "value": "100.5", "unit": "CNY", "period_key": "2026-06-30 00:00:00"}]

    def list_record_fields_for_records(self, record_ids, *, limit=None):
        return self.list_record_fields(record_version_id="record-1", limit=limit)


class _Repository:
    def get_raw_resource_snapshot(self, snapshot_id):
        assert snapshot_id == "snapshot-1"
        return SimpleNamespace(
            snapshot_id=snapshot_id,
            sha256="a" * 64,
            canonical_url="https://example.invalid/response",
            canonical_resource_id=None,
            created_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
            available_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
            published_at=None,
        )


def test_materialization_is_deterministic_and_keeps_field_lineage():
    materializer = StructuredFactMaterializer(_Storage(), _Repository())
    first = materializer.materialize("run-1")
    second = materializer.materialize("run-1")

    assert first.materialization_hash == second.materialization_hash
    assert first.facts[0].fact_id == second.facts[0].fact_id
    assert first.facts[0].value == 100.5
    assert first.facts[0].verification_status.value == "供应商直采"
    assert first.facts[0].structured_admission.field_path == "$.TOTAL_OPERATE_INCOME"
    assert first.facts[0].structured_admission.raw_resource_snapshot_id == "snapshot-1"
    assert first.facts[0].metadata["structured_row_key"] == "row-1"


def test_strict_historical_materialization_excludes_late_records():
    materializer = StructuredFactMaterializer(_Storage(), _Repository())
    result = materializer.materialize(
        "run-1",
        as_of=datetime(2026, 9, 9, tzinfo=timezone.utc),
        strict_historical=True,
    )

    assert result.facts == ()
    assert result.skipped == 1
