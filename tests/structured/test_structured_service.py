from datetime import datetime, timezone

from analysis.models import SyncRequest
from analysis.structured.service import StructuredDataService


def test_bound_service_loads_full_registry_and_zero_network_plan(tmp_path):
    service = StructuredDataService.create(tmp_path / "isolated.db", tmp_path / "data")
    assert service.registry(limit=500, offset=0)["total"] == 55
    assert service.fields(limit=5000, offset=0)["total"] == 2504

    preview = service.plan(
        "600519",
        mode="baseline",
        company_scope="company-only",
        datasets=("balance_fields",),
        as_of=datetime(2026, 9, 8, tzinfo=timezone.utc),
    )
    assert preview["company_count"] == 1
    assert preview["dataset_count"] == 1
    assert preview["performed_network_io"] is False
    assert preview["persisted"] is True
    assert preview["execution_boundary"] == "durable_plan_only"
    assert preview["run_ids"]


def test_cache_miss_lists_identity_prerequisite_and_known_sync_is_durably_queued(tmp_path):
    service = StructuredDataService.create(tmp_path / "isolated.db", tmp_path / "data")
    preview = service.plan("601999", mode="incremental")
    assert preview["runnable"] is False
    assert preview["identity_prerequisites"] == ["B08", "C01"]

    result = service.sync(
        "600519", SyncRequest(source_strategy="structured-first-v1")
    )
    assert result.acquisition_status == "planned"
    assert result.acquisition_run_id
    assert result.structured_plan_id
    assert result.structured_dataset_coverage["persisted"] is True
    assert result.structured_dataset_coverage["dataset_count"] == 31
