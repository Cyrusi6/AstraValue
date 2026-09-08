from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from analysis.structured.registry import StructuredRegistryLoader
from analysis.structured.runtime import _bind_parameters


ROOT = Path(__file__).resolve().parents[2]
DATASETS_PATH = ROOT / "config" / "structured_data" / "datasets.v1.json"
NOW = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
IDENTITY = {"security_code": "600519", "market": "SSE"}
JOB = {
    "purpose": "fetch",
    "time_start": "2025-01-01T00:00:00+00:00",
    "time_end": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture(scope="module")
def registry():
    return StructuredRegistryLoader().load()


@pytest.mark.parametrize(
    ("dataset_id", "parameter", "expected"),
    [
        ("company_basic", "filter", '(SECUCODE="600519.SH")'),
        ("em_metrics", "filter", '(SECUCODE="600519.SH")'),
        ("holder_count", "filter", '(SECURITY_CODE="600519")'),
        ("repurchase", "filter", '(DIM_SCODE="600519")'),
        ("balance_fields", "code", "SH600519"),
        ("market_cap", "secid", "1.600519"),
        ("baostock_daily", "code", "sh.600519"),
    ],
)
def test_final_parameter_binding_uses_provider_specific_security_shape(
    registry, dataset_id: str, parameter: str, expected: str
):
    dataset = registry.dataset(dataset_id).model_dump(mode="json")
    params = _bind_parameters(
        dataset,
        IDENTITY,
        JOB,
        page_number=1,
        plan_parameters={"report_period": "2025-12-31"},
        resolved_company_type="4",
    )

    assert params[parameter] == expected


def test_registry_locks_all_eastmoney_filter_placeholders():
    payload = json.loads(DATASETS_PATH.read_text(encoding="utf-8"))
    filters = [
        (item["dataset_id"], item["request"].get("parameter_template", {}).get("filter"))
        for item in payload["datasets"]
    ]

    secucode = [
        (dataset_id, value)
        for dataset_id, value in filters
        if value and "SECUCODE=" in value
    ]
    security_code = [
        (dataset_id, value)
        for dataset_id, value in filters
        if value and "SECURITY_CODE=" in value
    ]
    dim_scode = [
        (dataset_id, value)
        for dataset_id, value in filters
        if value and "DIM_SCODE=" in value
    ]

    assert len(secucode) == 29
    assert len(security_code) == 7
    assert len(dim_scode) == 1
    assert all(value == '(SECUCODE="{provider_code}")' for _, value in secucode)
    assert all(value == '(SECURITY_CODE="{security_code}")' for _, value in security_code)
    assert all(value == '(DIM_SCODE="{security_code}")' for _, value in dim_scode)
    assert all("supplier_security_code" not in value for _, value in filters if value)
