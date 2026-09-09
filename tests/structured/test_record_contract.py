from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from analysis.structured.records import stable_row_key
from analysis.structured.registry import StructuredRegistryLoader
from analysis.structured.runtime import _bind_parameters


EXPECTED_KEYS = {
    "holders_history": ("SECUCODE", "END_DATE", "HOLDER_NAME"),
    "float_holders_history": ("SECUCODE", "END_DATE", "HOLDER_NAME"),
    "management_roster": ("SECUCODE", "PERSON_CODE"),
    "block_trade": ("SECUCODE", "TRADE_DATE", "DAILY_RANK"),
    "fund_holds": ("SECUCODE", "REPORT_DATE", "HOLDER_CODE"),
    "institution_holds": ("SECUCODE", "REPORT_DATE", "ORG_TYPE"),
    "segments": ("SECUCODE", "REPORT_DATE", "ITEM_CODE", "MAINOP_TYPE"),
    "staff_structure": ("SECUCODE", "REPORT_DATE", "DISTRIBUTION_NAME"),
    "subsidiaries": ("SECUCODE", "REPORT_DATE", "HOLD_ORG_NAME"),
    "controller": ("SECUCODE", "HOLDER_NAME"),
    "repurchase": ("SECUCODE", "REPURCODE"),
    "violation": ("SECUCODE", "NOTICE_DATE", "PUNISH_OBJECT", "PUNISH_TYPE"),
    "baostock_calendar": ("calendar_date",),
    "baostock_adjust": ("code", "dividOperateDate"),
    "company_basic": ("SECUCODE",),
    "tags": ("SECUCODE", "BOARD_CODE"),
    "macro_cpi": ("REPORT_DATE",),
    "macro_retail": ("REPORT_DATE",),
    "surveys": (
        "SECUCODE",
        "NOTICE_DATE",
        "NUM",
        "RECEIVE_OBJECT",
        "RECEIVE_START_DATE",
    ),
}


def test_registry_row_keys_are_explicit_non_synthetic_contracts():
    bundle = StructuredRegistryLoader().load()
    by_id = {item.dataset_id: item for item in bundle.datasets.datasets}
    assert len(bundle.datasets.datasets) == 55
    for dataset in bundle.datasets.datasets:
        assert dataset.primary_key_fields
        assert not any(name.startswith("__") for name in dataset.primary_key_fields)
    for dataset_id, expected in EXPECTED_KEYS.items():
        assert by_id[dataset_id].primary_key_fields == expected


def test_realistic_rows_reject_old_keys_and_keep_new_keys_unique():
    rows_by_dataset = {
        "baostock_calendar": [
            {"calendar_date": "2020-01-01"},
            {"calendar_date": "2020-01-02"},
        ],
        "baostock_adjust": [
            {"code": "sh.600519", "dividOperateDate": "2020-06-10"},
            {"code": "sh.600519", "dividOperateDate": "2021-06-10"},
        ],
        "tags": [
            {"SECUCODE": "600519.SH", "BOARD_CODE": "BK001"},
            {"SECUCODE": "600519.SH", "BOARD_CODE": "BK002"},
        ],
        "block_trade": [
            {"SECUCODE": "600519.SH", "TRADE_DATE": "2020-01-02", "DAILY_RANK": 1},
            {"SECUCODE": "600519.SH", "TRADE_DATE": "2020-01-02", "DAILY_RANK": 2},
        ],
        "segments": [
            {"SECUCODE": "600519.SH", "REPORT_DATE": "2020-12-31", "ITEM_CODE": "A", "MAINOP_TYPE": "主营"},
            {"SECUCODE": "600519.SH", "REPORT_DATE": "2020-12-31", "ITEM_CODE": "A", "MAINOP_TYPE": "其他"},
        ],
    }
    for dataset_id, rows in rows_by_dataset.items():
        new_keys = EXPECTED_KEYS[dataset_id]
        values = [stable_row_key(dataset_id, row, new_keys) for row in rows]
        assert len(values) == len(set(values))
    with pytest.raises(ValueError, match="__company_id"):
        stable_row_key("baostock_calendar", rows_by_dataset["baostock_calendar"][0], ("__company_id",))
    with pytest.raises(ValueError, match="__retrieved_at"):
        stable_row_key("tags", rows_by_dataset["tags"][0], ("SECUCODE", "__retrieved_at"))


def test_page_size_overrides_are_dataset_specific_and_bind_into_requests():
    bundle = StructuredRegistryLoader().load()
    surveys = bundle.dataset("surveys").model_dump(mode="json")
    fund_holds = bundle.dataset("fund_holds").model_dump(mode="json")
    assert surveys["request"]["page_size"] == 50
    assert fund_holds["request"]["page_size"] == 100
    raw = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "structured_data" / "datasets.v1.json").read_text(encoding="utf-8")
    )
    assert all(
        "page_size" not in item["request"]
        for item in raw["datasets"]
        if item["dataset_id"] not in {"surveys", "fund_holds"}
    )
    identity = {"market": "SSE", "security_code": "600519"}
    job = {
        "time_start": datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
        "time_end": datetime(2021, 1, 1, tzinfo=timezone.utc).isoformat(),
        "purpose": "dataset",
    }
    assert _bind_parameters(surveys, identity, job, page_number=1)["pageSize"] == "50"
    assert _bind_parameters(fund_holds, identity, job, page_number=1)["pageSize"] == "100"


def test_surveys_key_and_sort_form_a_stable_business_total_order():
    bundle = StructuredRegistryLoader().load()
    surveys = bundle.dataset("surveys")
    assert tuple(surveys.primary_key_fields) == EXPECTED_KEYS["surveys"]
    fixed = surveys.request.fixed_parameters
    sort_columns = tuple(fixed["sortColumns"].split(","))
    assert sort_columns == EXPECTED_KEYS["surveys"][1:]
    assert tuple(fixed["sortTypes"].split(",")) == ("-1",) * len(sort_columns)
    assert "EUTIME" not in sort_columns


def test_fund_holds_sort_covers_report_period_and_row_identity():
    bundle = StructuredRegistryLoader().load()
    fund_holds = bundle.dataset("fund_holds")
    fixed = fund_holds.request.fixed_parameters
    sort_columns = tuple(fixed["sortColumns"].split(","))
    assert sort_columns == ("REPORT_DATE", "TOTAL_SHARES", "HOLDER_CODE")
    assert set(EXPECTED_KEYS["fund_holds"][1:]).issubset(sort_columns)
    assert tuple(fixed["sortTypes"].split(",")) == ("-1",) * len(sort_columns)
