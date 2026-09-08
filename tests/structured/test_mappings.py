from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from analysis.structured.mappings import (
    DATASET_PROTOCOL_SUPPORT,
    FALLBACK_SPECS,
    FIELD_RULES,
    KNOWN_MAPPING_GAPS,
    CandidateValue,
    FieldRoute,
    ProtocolFamily,
    aggregation_level,
    classify_field,
    entity_reference,
    execute_fallback,
    holding_ratio_basis,
    holding_record_key,
    lifecycle_state,
    map_row,
    map_counterparty_record,
    record_dates,
    record_dimensions,
    resolve_fields,
    select_leaf_segments,
    validate_protocol_support,
)
from analysis.structured.records import DataNature, PeriodKind


UTC = timezone.utc
NOW = datetime(2025, 12, 31, tzinfo=UTC)


def test_all_55_planning_datasets_have_executable_protocol_support() -> None:
    document = json.loads(
        Path("docs/acquisition/structured-data-interface-fields-v1.json").read_text(encoding="utf-8")
    )
    validate_protocol_support(document["datasets"])
    assert len(DATASET_PROTOCOL_SUPPORT) == document["dataset_count"] == 55
    assert set(DATASET_PROTOCOL_SUPPORT) == {item["dataset_id"] for item in document["datasets"]}
    assert all(item.state == "executable" for item in DATASET_PROTOCOL_SUPPORT.values())


def test_protocol_support_covers_each_approved_transport_family() -> None:
    families = {item.protocol for item in DATASET_PROTOCOL_SUPPORT.values()}
    assert families == {
        ProtocolFamily.EM_S,
        ProtocolFamily.EM_W,
        ProtocolFamily.EM_M,
        ProtocolFamily.EM_F,
        ProtocolFamily.EM_Q,
        ProtocolFamily.BAOSTOCK,
    }
    assert DATASET_PROTOCOL_SUPPORT["segments"].protocol is ProtocolFamily.EM_S
    assert DATASET_PROTOCOL_SUPPORT["em_metrics"].protocol is ProtocolFamily.EM_M
    assert DATASET_PROTOCOL_SUPPORT["market_cap"].protocol is ProtocolFamily.EM_Q


def test_protocol_support_rejects_registry_group_drift() -> None:
    entry = {"dataset_id": "segments", "plan_group_id": "WRONG"}
    support = {"segments": DATASET_PROTOCOL_SUPPORT["segments"]}
    try:
        validate_protocol_support([entry], support=support)
    except ValueError as exc:
        assert "plan group mismatch" in str(exc)
    else:
        raise AssertionError("group drift must fail closed")


def test_confirmed_financial_routes_keep_distinct_revenue_and_pcf_semantics() -> None:
    total = FIELD_RULES[("income_fields", "TOTAL_OPERATE_INCOME")]
    operating = FIELD_RULES[("income_fields", "OPERATE_INCOME")]
    pcf = FIELD_RULES[("baostock_daily", "pcfNcfTTM")]
    assert total.standard_field != operating.standard_field
    assert pcf.standard_field == "pcf_net_cashflow_ttm"
    assert FIELD_RULES[("baostock_profit", "epsTTM")].period_kind is PeriodKind.TTM


def test_percent_and_per_ten_share_units_use_explicit_field_multipliers() -> None:
    daily = {item.raw_field: item for item in map_row("baostock_daily", {"turn": "12.5", "pctChg": "-2"})}
    dividend = map_row("dividend", {"PRETAX_BONUS_RMB": "25.9"})[0]
    assert daily["turn"].unit == "ratio"
    assert daily["turn"].normalized_numeric_value == Decimal("0.125")
    assert daily["pctChg"].normalized_numeric_value == Decimal("-0.02")
    assert dividend.unit == "CNY_per_share"
    assert dividend.normalized_numeric_value == Decimal("2.59")
    assert not dividend.formula_eligible  # proposal value is not an implemented cash payment


def test_baostock_overlap_fields_are_preserved_but_not_promoted_to_second_truth() -> None:
    fields = map_row(
        "baostock_profit",
        {"roeAvg": "0.3", "netProfit": "100", "MBRevenue": "200", "totalShare": "10"},
    )
    by_name = {item.raw_field: item for item in fields}
    assert by_name["roeAvg"].standard_field == "roe_average"
    assert by_name["netProfit"].standard_field is None
    assert by_name["MBRevenue"].standard_field is None
    assert by_name["totalShare"].standard_field is None


def test_unknown_upstream_field_is_preserved_and_never_formula_eligible() -> None:
    fields = map_row(
        "income_fields",
        {"OPERATE_INCOME": 1, "NEW_FIELD": 2},
        known_fields={"OPERATE_INCOME"},
    )
    unknown = next(item for item in fields if item.raw_field == "NEW_FIELD")
    assert unknown.raw_value == 2
    assert unknown.nature is DataNature.UNCLASSIFIED
    assert not unknown.formula_eligible


def test_text_label_forecast_estimate_and_plan_natures_are_isolated() -> None:
    assert classify_field("company_basic", "MAIN_BUSINESS") is DataNature.SOURCE_TEXT
    assert classify_field("pledge", "WARNING_LINE") is DataNature.PROVIDER_ESTIMATE
    assert classify_field("capital_projects", "YIELD") is DataNature.FORECAST
    assert classify_field("capital_projects", "PLAN_INVEST_AMT") is DataNature.ANNOUNCED_PLAN
    assert classify_field("tags", "BOARD_NAME") is DataNature.PLATFORM_LABEL
    assert classify_field("forecasts", "EPS1") is DataNature.FORECAST
    assert classify_field("repurchase", "REPURAMOUNTLIMIT") is DataNature.ANNOUNCED_PLAN
    assert classify_field("repurchase", "REPURAMOUNT") is DataNature.OBSERVED


def test_known_objective_field_can_be_mapped_while_text_remains_nonformula() -> None:
    fields = map_row("company_basic", {"ORG_NAME": "贵州茅台", "MAIN_BUSINESS": "白酒"})
    assert len(fields) == 2
    assert all(not item.formula_eligible for item in fields)
    assert {item.nature for item in fields} == {DataNature.OBSERVED, DataNature.SOURCE_TEXT}


def test_segment_leaf_selection_avoids_parent_child_double_counting() -> None:
    rows = [
        {"SECUCODE": "600519.SH", "REPORT_DATE": "2025-12-31", "MAINOP_TYPE": "产品", "ITEM_CODE": "wine", "ITEM_PARENT_CODE": None, "ITEM_LEVEL": 1, "ITEM_NAME": "酒类"},
        {"SECUCODE": "600519.SH", "REPORT_DATE": "2025-12-31", "MAINOP_TYPE": "产品", "ITEM_CODE": "maotai", "ITEM_PARENT_CODE": "wine", "ITEM_LEVEL": 2, "ITEM_NAME": "茅台酒"},
        {"SECUCODE": "600519.SH", "REPORT_DATE": "2025-12-31", "MAINOP_TYPE": "地区", "ITEM_CODE": "domestic", "ITEM_PARENT_CODE": None, "ITEM_LEVEL": 1, "ITEM_NAME": "国内"},
    ]
    leaves = select_leaf_segments(rows)
    assert {row["ITEM_CODE"] for row in leaves} == {"maotai", "domestic"}
    assert len(rows) == 3  # source records are not mutated or discarded


def test_holding_summary_and_detail_have_separate_aggregation_levels() -> None:
    assert aggregation_level("institution_holds") == "institution_summary"
    assert aggregation_level("fund_holds") == "fund_detail"


def test_known_structured_gaps_are_not_silently_promoted() -> None:
    assert set(KNOWN_MAPPING_GAPS) >= {
        "complete_management_tenure",
        "comprehensive_related_transactions",
        "parent_company_statements",
        "bond_lifecycle",
    }
    assert "仅表示关联担保" in KNOWN_MAPPING_GAPS["comprehensive_related_transactions"].reason


def test_registered_fallbacks_record_real_upstream_and_validation_boundary() -> None:
    assert FALLBACK_SPECS["eastmoney_market_history"].wrapper == "stock_zh_a_hist"
    assert FALLBACK_SPECS["sina_financial_statements"].upstream == "vip.stock.finance.sina.com.cn"
    assert FALLBACK_SPECS["eastmoney_same_meaning_metrics"].compatible_fields == {"roic"}


def test_fallback_is_not_called_until_requested_field_definition_is_validated() -> None:
    called = 0

    def caller(**params):
        nonlocal called
        called += 1
        return [{"收盘": 100}]

    result = execute_fallback(
        "eastmoney_market_history",
        caller,
        params={"symbol": "600519", "period": "daily"},
        requested_fields={"market_close"},
    )
    assert called == 0
    assert result.gaps == {"market_close": "trigger_sample_not_validated"}


def test_fallback_returns_only_requested_compatible_fields() -> None:
    result = execute_fallback(
        "eastmoney_market_history",
        lambda **params: [{"日期": "2025-12-31", "收盘": 100, "开盘": 99}],
        params={"symbol": "600519", "period": "daily", "adjust": ""},
        requested_fields={"market_close"},
        validated_fields={"market_close"},
    )
    assert result.values == {"market_close": (100,)}
    assert "market_open" not in result.values


def test_mismatched_fallback_semantics_stay_a_gap_without_io() -> None:
    called = 0

    def caller(**params):
        nonlocal called
        called += 1
        return [{"ROEJQ": 1}]

    result = execute_fallback(
        "eastmoney_same_meaning_metrics",
        caller,
        params={"type": "RPT", "sty": "APP", "filter": "x", "p": 1, "ps": 20, "sr": -1, "st": "REPORT_DATE"},
        requested_fields={"roe_average"},
        validated_fields={"roe_average"},
    )
    assert called == 0
    assert result.gaps["roe_average"] == "definition_or_period_mismatch"


def _candidate(field_id: str, dataset: str, value, *, definition="def-v1", valid=True) -> CandidateValue:
    return CandidateValue(
        fact_id=f"fact-{dataset}-{field_id}",
        field_id=field_id,
        value=value,
        source_dataset=dataset,
        definition_id=definition,
        period_kind=PeriodKind.CUMULATIVE,
        scope="consolidated",
        available_at=NOW - timedelta(days=10),
        valid_until=NOW + timedelta(days=10) if valid else NOW - timedelta(days=1),
    )


def _routes() -> list[FieldRoute]:
    return [
        FieldRoute("revenue", "income_fields", ("sina",), "def-v1", PeriodKind.CUMULATIVE, "consolidated"),
        FieldRoute("cashflow", "cashflow_fields", ("sina",), "def-v1", PeriodKind.CUMULATIVE, "consolidated"),
    ]


def test_valid_primary_cache_avoids_all_requests() -> None:
    calls = []
    result = resolve_fields(
        _routes(),
        as_of=NOW,
        cache={"revenue": _candidate("revenue", "income_fields", 1), "cashflow": _candidate("cashflow", "cashflow_fields", 2)},
        fetch=lambda dataset, fields: calls.append((dataset, fields)) or {},
    )
    assert set(result.values) == {"revenue", "cashflow"}
    assert calls == []


def test_primary_requests_are_coalesced_by_dataset() -> None:
    routes = [
        FieldRoute("revenue", "income_fields", (), "def-v1", PeriodKind.CUMULATIVE, "consolidated"),
        FieldRoute("profit", "income_fields", (), "def-v1", PeriodKind.CUMULATIVE, "consolidated"),
    ]
    calls = []

    def fetch(dataset, fields):
        calls.append((dataset, fields))
        return {field: _candidate(field, dataset, index) for index, field in enumerate(fields)}

    result = resolve_fields(routes, as_of=NOW, cache={}, fetch=fetch)
    assert len(calls) == 1 and calls[0][1] == {"revenue", "profit"}
    assert set(result.values) == {"revenue", "profit"}


def test_fallback_fills_only_the_missing_field_and_never_overwrites_primary() -> None:
    calls = []

    def fetch(dataset, fields):
        calls.append((dataset, fields))
        if dataset == "income_fields":
            return {"revenue": _candidate("revenue", dataset, 100)}
        if dataset == "cashflow_fields":
            return {}
        return {
            "revenue": _candidate("revenue", dataset, 999),
            "cashflow": _candidate("cashflow", dataset, 50),
        }

    result = resolve_fields(_routes(), as_of=NOW, cache={}, fetch=fetch)
    assert result.values["revenue"].value == 100
    assert result.values["cashflow"].value == 50
    fallback_call = next(call for call in result.calls if call.purpose == "fallback")
    assert fallback_call.field_ids == ("cashflow",)


def test_stale_cache_does_not_masquerade_as_current() -> None:
    result = resolve_fields(
        [_routes()[0]],
        as_of=NOW,
        cache={"revenue": _candidate("revenue", "income_fields", 1, valid=False)},
        fetch=lambda dataset, fields: {},
    )
    assert "revenue" not in result.values
    assert result.gaps["revenue"] == "no_valid_value"


def test_definition_mismatch_triggers_fallback_instead_of_acceptance() -> None:
    def fetch(dataset, fields):
        if dataset == "income_fields":
            return {"revenue": _candidate("revenue", dataset, 1, definition="different")}
        return {"revenue": _candidate("revenue", dataset, 2)}

    result = resolve_fields([_routes()[0]], as_of=NOW, cache={}, fetch=fetch)
    assert result.values["revenue"].value == 2
    assert result.fallback_reasons["revenue"] == "primary_validation_failed"


def test_c_records_keep_anonymous_counterparties_and_dimensions_without_inventing_identity() -> None:
    anonymous = map_counterparty_record(
        {
            "SECUCODE": "600519.SH",
            "REPORT_DATE": "2025-12-31",
            "TYPE": "客户",
            "RANK": 1,
            "ITEM_NAME": "第一名客户",
            "AMOUNT": 100,
            "SUM_AMOUNT": 500,
            "TOI_RATIO": 12.5,
        }
    )
    disclosed = map_counterparty_record(
        {
            "SECUCODE": "600519.SH",
            "REPORT_DATE": "2025-12-31",
            "TYPE": "供应商",
            "RANK": 1,
            "ITEM_NAME": "贵州供应链有限公司",
            "AMOUNT": 80,
        }
    )
    assert anonymous.counterparty.resolution == "anonymous"
    assert anonymous.counterparty.identity_key is None
    assert anonymous.amount == 100 and anonymous.source_ratio == 12.5
    assert ("TYPE", "客户") in anonymous.dimensions and ("RANK", "1") in anonymous.dimensions
    assert disclosed.counterparty.resolution == "name_only"
    assert disclosed.counterparty.identity_key is None  # a source name is not a resolved legal identity


def test_c_segment_dimensions_and_resume_text_are_not_collapsed_into_amount_facts() -> None:
    product = {
        "SECUCODE": "600519.SH",
        "REPORT_DATE": "2025-12-31",
        "MAINOP_TYPE": "产品",
        "ITEM_CODE": "001",
        "ITEM_LEVEL": 1,
    }
    region = {**product, "MAINOP_TYPE": "地区"}
    assert record_dimensions("segments", product) != record_dimensions("segments", region)
    resume = map_row(
        "management_roster",
        {"PERSON_CODE": "p1", "PERSON_NAME": "张三", "RESUME": "曾任某公司董事"},
    )
    by_name = {item.raw_field: item for item in resume}
    assert by_name["RESUME"].nature is DataNature.SOURCE_TEXT
    assert not by_name["RESUME"].formula_eligible
    assert by_name["PERSON_CODE"].raw_value == "p1"


def test_g_subject_codes_prevent_same_name_merge_and_name_only_stays_unresolved() -> None:
    first = entity_reference(
        {"PERSON_CODE": "P001", "PERSON_NAME": "张三"},
        code_fields=("PERSON_CODE",),
        name_fields=("PERSON_NAME",),
    )
    second = entity_reference(
        {"PERSON_CODE": "P002", "PERSON_NAME": "张三"},
        code_fields=("PERSON_CODE",),
        name_fields=("PERSON_NAME",),
    )
    name_only = entity_reference(
        {"PERSON_NAME": "张三"},
        code_fields=("PERSON_CODE",),
        name_fields=("PERSON_NAME",),
    )
    assert first.identity_key != second.identity_key
    assert name_only.resolution == "name_only" and name_only.identity_key is None


def test_g_holding_denominators_dates_and_goodwill_balance_are_kept_distinct() -> None:
    total_holder = map_row("holders_history", {"HOLD_NUM_RATIO": 10})[0]
    float_holder = map_row("float_holders_history", {"FREE_HOLDNUM_RATIO": 10})[0]
    assert holding_ratio_basis("holders_history", "HOLD_NUM_RATIO") == "total_shares_unconfirmed"
    assert holding_ratio_basis("float_holders_history", "FREE_HOLDNUM_RATIO") == "free_float_shares"
    assert not total_holder.formula_eligible
    assert float_holder.normalized_numeric_value == Decimal("0.10")

    dates = record_dates(
        "management_roster",
        {
            "INCUMBENT_DATE": "2021-01-01",
            "INCUMBENT_TIME": "2021-2024",
            "REPORT_DATE": "2024-12-31",
        },
    )
    assert dates == {
        "incumbent_start": "2021-01-01",
        "incumbent_source_period": "2021-2024",
        "report_date": "2024-12-31",
    }

    goodwill = {item.raw_field: item for item in map_row("goodwill", {"GOODWILL": 100, "GOODWILL_CHANGE": 20})}
    assert goodwill["GOODWILL"].value_kind.value == "stock"
    assert goodwill["GOODWILL_CHANGE"].value_kind.value == "flow"
    assert goodwill["GOODWILL"].standard_field != goodwill["GOODWILL_CHANGE"].standard_field


def test_a_lifecycle_and_natures_keep_plan_implementation_warning_and_forecast_separate() -> None:
    proposed = lifecycle_state("dividend", {"ASSIGN_PROGRESS": "董事会预案"})
    implemented = lifecycle_state("dividend", {"ASSIGN_PROGRESS": "实施完成"})
    terminated = lifecycle_state("repurchase", {"REPURPROGRESS": "终止实施"})
    assert (proposed.phase, implemented.phase, terminated.phase) == (
        "planned",
        "completed",
        "terminated",
    )
    assert proposed.source_value == "董事会预案" and implemented.source_value == "实施完成"

    dividend = map_row("dividend", {"PRETAX_BONUS_RMB": 10})[0]
    repurchase = {item.raw_field: item for item in map_row("repurchase", {"REPURAMOUNTLIMIT": 100, "REPURAMOUNT": 60})}
    pledge = map_row("pledge", {"WARNING_LINE": 120})[0]
    project = {item.raw_field: item for item in map_row("capital_projects", {"PLAN_INVEST_AMT": 100, "ACTUAL_INPUT_RF": 40, "YIELD": 8})}
    assert dividend.normalized_numeric_value == Decimal("1.0") and not dividend.formula_eligible
    assert repurchase["REPURAMOUNTLIMIT"].nature is DataNature.ANNOUNCED_PLAN
    assert repurchase["REPURAMOUNT"].nature is DataNature.OBSERVED
    assert pledge.nature is DataNature.PROVIDER_ESTIMATE and not pledge.formula_eligible
    assert project["PLAN_INVEST_AMT"].nature is DataNature.ANNOUNCED_PLAN
    assert project["ACTUAL_INPUT_RF"].nature is DataNature.OBSERVED
    assert project["YIELD"].nature is DataNature.FORECAST
    assert not any(item.formula_eligible for item in project.values())


def test_t_holding_summary_and_detail_keys_do_not_double_count_same_company_period() -> None:
    summary = holding_record_key(
        "institution_holds",
        {"SECUCODE": "600519.SH", "REPORT_DATE": "2025-12-31", "ORG_TYPE": "基金"},
    )
    detail = holding_record_key(
        "fund_holds",
        {
            "SECUCODE": "600519.SH",
            "REPORT_DATE": "2025-12-31",
            "FUND_CODE": "000001",
            "HOLDER_CODE": "H01",
        },
    )
    assert summary[0] == "institution_summary"
    assert detail[0] == "fund_detail"
    assert summary != detail


def test_i_l_p_records_preserve_raw_values_but_do_not_promote_unknown_text_or_forecasts() -> None:
    macro = map_row("macro_cpi", {"REPORT_DATE": "2025-12", "NATIONAL_SAME": 1.2})
    label = map_row("tags", {"BOARD_NAME": "白酒", "BOARD_YIELD": 8})
    forecast = map_row("forecasts", {"EPS1": 10, "RATING": "买入"})
    survey = map_row("surveys", {"CONTENT": "调研原文", "NOTICE_DATE": "2025-12-31"})
    assert {item.raw_field for item in macro} == {"REPORT_DATE", "NATIONAL_SAME"}
    assert all(not item.formula_eligible for item in macro)
    assert all(item.nature is DataNature.PLATFORM_LABEL for item in label)
    assert all(item.nature is DataNature.FORECAST for item in forecast)
    assert next(item for item in survey if item.raw_field == "CONTENT").nature is DataNature.SOURCE_TEXT
    assert not any(item.formula_eligible for items in (label, forecast, survey) for item in items)
