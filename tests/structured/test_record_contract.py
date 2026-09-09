from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from analysis.structured.records import stable_row_key
from analysis.structured.protocols import (
    EastmoneyRequest,
    ProtocolFamily,
    ResultStatus,
    parse_eastmoney_response,
)
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
    "market_cap": ("SECUCODE", "TRADE_DATE"),
    "capital_projects": ("SECURITY_CODE", "NOTICE_DATE", "ORG_CODE", "ITEM_NAME"),
    "customers_peer": ("SECUCODE", "REPORT_DATE", "ORG_CODE", "TYPE_CODE", "RANK"),
    "guarantee": ("SECUCODE", "EID_EID"),
    "litigation": (
        "SECUCODE",
        "NOTICE_DATE",
        "ORG_CODE",
        "CASE_NAME",
        "DEFENCE",
        "CASE_PROFILE",
    ),
    "pledge": ("SECUCODE", "MXID"),
    "management_trades": ("SECURITY_CODE", "GGEID"),
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


def test_row_key_collapse_batch_a_and_ordering_batch_b_contracts():
    bundle = StructuredRegistryLoader().load()
    expected_sorts = {
        "capital_projects": ("NOTICE_DATE", "ORG_CODE", "ITEM_NAME"),
        "customers_peer": ("REPORT_DATE", "ORG_CODE", "TYPE_CODE", "RANK"),
        "guarantee": ("EID_EID",),
        "litigation": ("NOTICE_DATE", "ORG_CODE", "CASE_NAME", "DEFENCE"),
        "pledge": ("MXID",),
        "management_trades": ("GGEID",),
        "segments": ("REPORT_DATE", "ITEM_CODE", "MAINOP_TYPE"),
        "institution_holds": ("REPORT_DATE", "ORG_TYPE"),
    }
    batch_a = {
        "capital_projects",
        "customers_peer",
        "guarantee",
        "litigation",
        "pledge",
        "management_trades",
    }
    batch_b = {"segments", "institution_holds"}
    assert set(expected_sorts) == batch_a | batch_b

    for dataset_id, expected_key in EXPECTED_KEYS.items():
        if dataset_id not in expected_sorts:
            continue
        dataset = bundle.dataset(dataset_id)
        assert tuple(dataset.primary_key_fields) == expected_key
        fixed = dataset.request.fixed_parameters
        sort_columns = tuple(str(fixed["sortColumns"]).split(","))
        assert sort_columns == expected_sorts[dataset_id]
        assert tuple(str(fixed["sortTypes"]).split(",")) == ("-1",) * len(sort_columns)
        filter_field = dataset.request.company_filter_field
        assert filter_field in {"SECUCODE", "SECURITY_CODE"}
        identity_filter_fields = {filter_field}
        # A few contracts filter by SECURITY_CODE while retaining the
        # provider's SECUCODE in the row identity; both identify the company
        # and neither needs to be repeated in the server-side ordering.
        if filter_field == "SECURITY_CODE":
            identity_filter_fields.add("SECUCODE")
        sortable_key = {
            field
            for field in expected_key
            if field not in identity_filter_fields
            # CASE_PROFILE is a valid identity component but Eastmoney rejects
            # it as a sort column (code=9501); keep it out of the request.
            and not (dataset_id == "litigation" and field == "CASE_PROFILE")
        }
        assert sortable_key.issubset(sort_columns)

    assert "REPORT_DATE" not in EXPECTED_KEYS["guarantee"]
    assert "CASE_PROFILE" not in expected_sorts["litigation"]


def test_row_key_collapse_contract_rejects_empty_identity_fields():
    bundle = StructuredRegistryLoader().load()
    guarantee = bundle.dataset("guarantee")
    with pytest.raises(ValueError, match="EID_EID"):
        stable_row_key(
            "guarantee",
            {"SECUCODE": "600519.SH", "EID_EID": ""},
            guarantee.primary_key_fields,
        )


def test_market_cap_history_key_sort_and_secucode_binding_contract():
    bundle = StructuredRegistryLoader().load()
    market_cap = bundle.dataset("market_cap")
    assert market_cap.request.protocol == "em_m"
    assert market_cap.history_mode == "all_available_history"
    assert market_cap.history_enumeration == "complete_pagination"
    assert tuple(market_cap.primary_key_fields) == EXPECTED_KEYS["market_cap"]
    rows = [
        {"SECUCODE": "600519.SH", "TRADE_DATE": "2026-09-08"},
        {"SECUCODE": "600519.SH", "TRADE_DATE": "2026-09-07"},
    ]
    keys = [
        stable_row_key("market_cap", row, market_cap.primary_key_fields)
        for row in rows
    ]
    assert len(keys) == len(set(keys))
    with pytest.raises(ValueError, match="TRADE_DATE"):
        stable_row_key(
            "market_cap",
            {"SECUCODE": "600519.SH", "TRADE_DATE": ""},
            market_cap.primary_key_fields,
        )
    fixed = market_cap.request.fixed_parameters
    sort_columns = tuple(str(fixed["st"]).split(","))
    assert set(EXPECTED_KEYS["market_cap"][1:]).issubset(sort_columns)
    assert fixed == {
        "type": "RPT_VALUEANALYSIS_DET",
        "sty": "ALL",
        "sr": "-1",
        "st": "TRADE_DATE",
        "source": "HSF10",
        "client": "PC",
    }
    source_plan = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "acquisition"
            / "structured-data-interface-fields-v1.json"
        ).read_text(encoding="utf-8")
    )
    source_market_cap = next(
        item for item in source_plan["datasets"] if item["dataset_id"] == "market_cap"
    )
    sample_params = source_market_cap["samples"][0]["params"]
    assert sample_params == {
        "type": "RPT_VALUEANALYSIS_DET",
        "sty": "ALL",
        "filter": '(SECUCODE="600519.SH")',
        "p": "1",
        "ps": "500",
        "sr": "-1",
        "st": "TRADE_DATE",
        "source": "HSF10",
        "client": "PC",
    }

    identity = {"market": "SSE", "security_code": "600519"}
    job = {
        "time_start": datetime(2018, 1, 1, tzinfo=timezone.utc).isoformat(),
        "time_end": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
        "purpose": "dataset",
    }
    params = _bind_parameters(
        market_cap.model_dump(mode="json"),
        identity,
        job,
        page_number=1,
    )
    assert params["filter"] == '(SECUCODE="600519.SH")'
    assert params["p"] == "1"
    assert params["ps"] == "500"


def test_market_cap_pagination_terminal_and_valid_empty_result_are_distinct():
    endpoint = "https://datacenter.eastmoney.com/securities/api/data/get"

    def request(page: int) -> EastmoneyRequest:
        return EastmoneyRequest(
            ProtocolFamily.EM_M,
            endpoint,
            {
                "type": "RPT_VALUEANALYSIS_DET",
                "sty": "ALL",
                "filter": '(SECUCODE="600519.SH")',
                "p": str(page),
                "ps": "500",
                "sr": "-1",
                "st": "TRADE_DATE",
                "source": "HSF10",
                "client": "PC",
            },
        )

    terminal = parse_eastmoney_response(
        request(5),
        status_code=200,
        body=json.dumps(
            {
                "success": True,
                "code": 0,
                "result": {
                    "data": [{"SECUCODE": "600519.SH", "TRADE_DATE": "2026-09-08"}],
                    "count": 2108,
                    "pages": 5,
                },
            }
        ).encode(),
    )
    assert terminal.status is ResultStatus.SUCCESS
    assert terminal.terminal is True
    assert terminal.declared_total == 2108
    assert terminal.declared_pages == 5

    empty = parse_eastmoney_response(
        request(1),
        status_code=200,
        body=json.dumps({"success": False, "code": 9201, "result": None}).encode(),
    )
    assert empty.status is ResultStatus.EMPTY
    assert empty.terminal is True
    assert empty.rows == ()


def test_market_cap_requirement_references_have_no_legacy_field_ids():
    root = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (
            root
            / "config"
            / "structured_data"
            / "research_requirements.v1.json"
        ).read_text(encoding="utf-8")
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    for legacy in tuple(
        f"market_cap.{suffix}" for suffix in ("f" + "116", "f" + "117", "f" + "86")
    ):
        assert legacy not in serialized
