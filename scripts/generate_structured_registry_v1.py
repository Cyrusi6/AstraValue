from __future__ import annotations

"""Generate v1 runtime registries from the frozen field evidence plus reviewed rules.

The planning JSON contributes only dataset identities, observed raw field locations and
evidence hashes. Executable request contracts are rebuilt below from protocol-specific
allowlists and placeholders; sampled company/date/page values are never copied.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "acquisition" / "structured-data-interface-fields-v1.json"
DESIGN_PATH = ROOT / "openspec" / "changes" / "structured-data-first-v1" / "design.md"
LEGACY_QUESTIONS_PATH = ROOT / "config" / "data_sources" / "business_model_questions.v1.json"
OUTPUT_DIR = ROOT / "config" / "structured_data"

SCHEMA_VERSION = "structured-registry.v1"
VERSION = "1.0.0"
REGISTRY_VERSIONS = {
    "datasets": "1.1.0",
    "fields": "1.1.0",
    "schedules": "1.1.0",
    "research_requirements": "1.1.0",
    "peer_sets": VERSION,
    "reading_rules": VERSION,
    "industry_profiles": VERSION,
}
GENERATED_FROM = (
    "docs/acquisition/structured-data-interface-fields-v1.json",
    "docs/acquisition/structured-data-field-plan-v1.md",
    "openspec/changes/structured-data-first-v1/design.md",
)

COMPANY_FILTER_FIELDS = {
    "holder_count": "SECURITY_CODE",
    "dividend": "SECURITY_CODE",
    "repurchase": "DIM_SCODE",
    "pledge": "SECURITY_CODE",
    "unlock_peer": "SECURITY_CODE",
    "management_trades": "SECURITY_CODE",
    "surveys": "SECURITY_CODE",
    "capital_projects": "SECURITY_CODE",
}
NO_COMPANY_FILTER = {"macro_cpi", "macro_retail", "baostock_calendar"}
SNAPSHOT_DATASETS = {"company_basic", "baostock_basic"}
ON_DEMAND_DATASETS = {"macro_cpi", "macro_retail", "tags", "forecasts"}
MARKET_DATASETS = {
    "market_cap",
    "baostock_adjust",
    "baostock_calendar",
    "baostock_daily",
}
FINANCIAL_GROUPS = {"F01", "F02", "F03", "F04", "F05", "B02", "B03", "B04", "B05", "B06", "B07"}
SNAPSHOT_GROUPS = {"C01", "M01"}
ON_DEMAND_GROUPS = {"I01", "I02", "L01", "P01"}

# 真实响应中默认识别字段可能为空或不唯一。按已核对的样本显式指定
# 非空且唯一的键组件，并拒绝把运行时不会注入的合成字段写入行身份。
KEY_FIELD_OVERRIDES: dict[str, tuple[str, ...]] = {
    "holders_history": ("SECUCODE", "END_DATE", "HOLDER_NAME"),
    "float_holders_history": ("SECUCODE", "END_DATE", "HOLDER_NAME"),
    "management_roster": ("SECUCODE", "PERSON_CODE"),
    "block_trade": ("SECUCODE", "TRADE_DATE", "DAILY_RANK"),
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
    "surveys": (
        "SECUCODE",
        "NOTICE_DATE",
        "NUM",
        "RECEIVE_OBJECT",
        "RECEIVE_START_DATE",
    ),
}

# 某些分页接口的原始样本排序不是行键的全序。对已实测存在跨页漂移的
# 数据集声明完整排序覆盖；排序方向沿用供应商样本的降序约定。
SORT_COLUMN_OVERRIDES: dict[str, tuple[str, ...]] = {
    "surveys": (
        "NOTICE_DATE",
        "NUM",
        "RECEIVE_OBJECT",
        "RECEIVE_START_DATE",
    ),
    "fund_holds": ("REPORT_DATE", "TOTAL_SHARES", "HOLDER_CODE"),
}

SORT_TIE_BREAKER_OVERRIDES: dict[str, tuple[str, ...]] = {
    "holders_history": ("HOLDER_NAME",),
    "float_holders_history": ("HOLDER_NAME",),
    "block_trade": ("DAILY_RANK",),
    "fund_holds": ("HOLDER_CODE",),
}

PAGE_SIZE_OVERRIDES: dict[str, int] = {
    "surveys": 50,
    "fund_holds": 100,
}

KNOWN_FIELD_REFS = set(
    re.findall(
        r"\b([A-Z]\d{2})\.([A-Za-z][A-Za-z0-9_]*)\b",
        DESIGN_PATH.read_text(encoding="utf-8"),
    )
)
F05_CANDIDATE_FIELDS = {
    "NET_INTEREST_MARGIN",
    "NON_PERFORMING_LOAN",
    "CAPITAL_PROVISIONS_SUM",
    "NEWCAPITALADER",
    "FIRST_ADEQUACY_RATIO",
    "LIQUIDITY_COVERAGE_RATIO",
    "NET_FUNDING_RATIO",
    "NHJZ_CURRENT_AMT",
    "NBV_LIFE",
    "NBV_RATE",
    "SOLVENCY_AR",
    "TOTAL_ROI",
    "RISK_COVERAGE",
    "NET_CAPITAL_LIABILITIES",
}
EXTRA_CONFIRMED_REFS = {
    ("B01", name)
    for name in (
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "turn",
        "tradestatus",
        "pctChg",
        "peTTM",
        "pbMRQ",
        "psTTM",
        "pcfNcfTTM",
        "isST",
    )
}
KNOWN_FIELD_REFS |= EXTRA_CONFIRMED_REFS


def _canonical_hash(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("content_sha256", None)
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _write(filename: str, payload: dict[str, Any]) -> None:
    payload["content_sha256"] = _canonical_hash(payload)
    path = OUTPUT_DIR / filename
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _envelope(kind: str, registry_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_kind": kind,
        "registry_id": registry_id,
        "version": REGISTRY_VERSIONS[kind],
        "content_sha256": "0" * 64,
        "generated_from": list(GENERATED_FROM),
    }


def _protocol(dataset: dict[str, Any]) -> str:
    endpoint = dataset["endpoint"]
    if dataset["provider"] == "baostock":
        return "baostock_sdk"
    if "NewFinanceAnalysis" in endpoint:
        return "em_f"
    if endpoint.endswith("/securities/api/data/get"):
        return "em_m"
    if "datacenter-web.eastmoney.com" in endpoint:
        return "em_w"
    if "datacenter.eastmoney.com/securities/api/data/v1/get" in endpoint:
        return "em_s"
    if "push2.eastmoney.com" in endpoint:
        return "em_q"
    raise ValueError(f"未知协议: {dataset['dataset_id']} {endpoint}")


def _safe_sort(dataset: dict[str, Any], key: str) -> str:
    value = str(dataset["samples"][0]["params"].get(key, ""))
    if value and not re.fullmatch(r"[A-Za-z0-9_,.-]+", value):
        raise ValueError(f"{dataset['dataset_id']}: 非法排序证据{key}={value!r}")
    return value


def _request_contract(dataset: dict[str, Any]) -> dict[str, Any]:
    dataset_id = dataset["dataset_id"]
    protocol = _protocol(dataset)
    report_name = dataset.get("report_name")
    if protocol in {"em_s", "em_w"}:
        allowed = [
            "reportName",
            "columns",
            "filter",
            "pageNumber",
            "pageSize",
            "sortColumns",
            "sortTypes",
            "source",
            "client",
        ]
        source = "WEB" if protocol == "em_w" else "HSF10"
        client = "WEB" if protocol == "em_w" else "PC"
        sort_override = SORT_COLUMN_OVERRIDES.get(dataset_id)
        if sort_override:
            sort_columns = ",".join(sort_override)
            sort_types = ",".join("-1" for _ in sort_override)
        else:
            sort_columns = _safe_sort(dataset, "sortColumns")
            sort_types = _safe_sort(dataset, "sortTypes")
            tie_breakers = SORT_TIE_BREAKER_OVERRIDES.get(dataset_id, ())
            if tie_breakers:
                existing_columns = tuple(
                    value for value in sort_columns.split(",") if value
                )
                existing_types = tuple(value for value in sort_types.split(",") if value)
                for tie_breaker in tie_breakers:
                    if tie_breaker not in existing_columns:
                        existing_columns += (tie_breaker,)
                        existing_types += ("-1",)
                sort_columns = ",".join(existing_columns)
                sort_types = ",".join(existing_types)
        fixed = {
            "reportName": report_name,
            "columns": "ALL",
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "source": source,
            "client": client,
        }
        template = {"pageNumber": "{page_number}", "pageSize": "{bounded_page_size}"}
        filter_field = None
        if dataset_id not in NO_COMPANY_FILTER:
            filter_field = COMPANY_FILTER_FIELDS.get(dataset_id, "SECUCODE")
            placeholder = (
                "{provider_code}" if filter_field == "SECUCODE" else "{security_code}"
            )
            template["filter"] = f'({filter_field}="{placeholder}")'
        contract = {
            "protocol": protocol,
            "method": "GET",
            "endpoint": dataset["endpoint"],
            "report_name": report_name,
            "allowed_parameters": allowed,
            "fixed_parameters": fixed,
            "parameter_template": template,
            "company_filter_field": filter_field,
            "provider_code_format": "{security_code}.{exchange}" if filter_field == "SECUCODE" else "{security_code}",
        }
        if dataset_id in PAGE_SIZE_OVERRIDES:
            contract["page_size"] = PAGE_SIZE_OVERRIDES[dataset_id]
        return contract
    if protocol == "em_m":
        fixed_parameters = {
            "type": report_name,
            "sty": "ALL" if dataset_id == "market_cap" else "APP_F10_MAINFINADATA",
            "sr": "-1",
            "st": "TRADE_DATE" if dataset_id == "market_cap" else "REPORT_DATE",
            "source": "HSF10",
            "client": "PC",
        }
        return {
            "protocol": protocol,
            "method": "GET",
            "endpoint": dataset["endpoint"],
            "report_name": report_name,
            "allowed_parameters": ["type", "sty", "filter", "p", "ps", "sr", "st", "source", "client"],
            "fixed_parameters": fixed_parameters,
            "parameter_template": {
                "filter": '(SECUCODE="{provider_code}")',
                "p": "{page_number}",
                "ps": "{bounded_page_size}",
            },
            "company_filter_field": "SECUCODE",
            "provider_code_format": "{security_code}.{exchange}",
        }
    if protocol == "em_f":
        report_type = "2" if dataset_id in {"income_quarter", "cashflow_quarter"} else "1"
        return {
            "protocol": protocol,
            "method": "GET",
            "endpoint": dataset["endpoint"],
            "report_name": None,
            "allowed_parameters": ["companyType", "reportDateType", "reportType", "dates", "code"],
            "fixed_parameters": {"reportDateType": "0", "reportType": report_type},
            "parameter_template": {
                "companyType": "{resolved_company_type}",
                "dates": "{catalog_report_dates}",
                "code": "{provider_code}",
            },
            "company_filter_field": "code",
            "provider_code_format": "{exchange}{security_code}",
        }
    if protocol == "em_q":
        return {
            "protocol": protocol,
            "method": "GET",
            "endpoint": dataset["endpoint"],
            "report_name": None,
            "allowed_parameters": ["secid", "fields", "fltt", "invt"],
            "fixed_parameters": {"fields": "f57,f58,f86,f116,f117", "fltt": "2", "invt": "2"},
            "parameter_template": {"secid": "{provider_secid}"},
            "company_filter_field": "secid",
            "provider_code_format": "{market_number}.{security_code}",
        }

    allowed_by_sdk = {
        "baostock_daily": ["code", "fields", "start_date", "end_date", "frequency", "adjustflag"],
        "baostock_adjust": ["code", "start_date", "end_date"],
        "baostock_basic": ["code"],
        "baostock_calendar": ["start_date", "end_date"],
    }
    allowed = allowed_by_sdk.get(dataset_id, ["code", "year", "quarter"])
    fixed: dict[str, str] = {}
    template: dict[str, str] = {}
    if dataset_id == "baostock_daily":
        fixed = {
            "fields": "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST",
            "frequency": "d",
            "adjustflag": "3",
        }
    for parameter in allowed:
        if parameter not in fixed:
            template[parameter] = "{" + parameter + "}"
    return {
        "protocol": protocol,
        "method": "SDK",
        "endpoint": dataset["endpoint"],
        "report_name": None,
        "allowed_parameters": allowed,
        "fixed_parameters": fixed,
        "parameter_template": template,
        "company_filter_field": None if dataset_id == "baostock_calendar" else "code",
        "provider_code_format": None if dataset_id == "baostock_calendar" else "{exchange_lower}.{security_code}",
    }


def _history(dataset: dict[str, Any]) -> tuple[str, str, str, str]:
    dataset_id = dataset["dataset_id"]
    group = dataset["plan_group_id"]
    protocol = _protocol(dataset)
    if dataset_id in SNAPSHOT_DATASETS:
        return "snapshot_from_first_retrieval", "current_snapshot", "none", "snapshot"
    if dataset_id in ON_DEMAND_DATASETS:
        enumeration = "date_range_batches" if dataset_id.startswith("macro_") else "on_demand_complete_pagination"
        pagination = "page_number" if protocol != "baostock_sdk" else "sdk_exhaustion"
        return "on_demand_all_available_history", enumeration, pagination, "on_demand"
    if protocol == "em_f":
        return "all_available_history", "financial_date_catalog", "financial_date_catalog", "financial"
    if protocol == "baostock_sdk":
        if group in {"B02", "B03", "B04", "B05", "B06", "B07"}:
            return "all_available_history", "year_quarter_batches", "sdk_exhaustion", "financial"
        return "all_available_history", "date_range_batches", "sdk_exhaustion", "market"
    update = "financial" if group in FINANCIAL_GROUPS else "market" if dataset_id in MARKET_DATASETS else "event"
    return "all_available_history", "complete_pagination", "page_number", update


def _raw_names(dataset: dict[str, Any]) -> set[str]:
    return {item["name"] for item in dataset["fields"]}


def _record_contract(dataset: dict[str, Any]) -> tuple[list[str], list[str]]:
    names = _raw_names(dataset)
    company = next((name for name in ("SECUCODE", "SECURITY_CODE", "code", "f57") if name in names), "__company_id")
    date_candidates = [
        name
        for name in (
            "REPORT_DATE",
            "END_DATE",
            "TRADE_DATE",
            "NOTICE_DATE",
            "PUBLISH_DATE",
            "CHANGE_DATE",
            "FREE_DATE",
            "date",
            "statDate",
            "calendar_date",
            "f86",
            "ipoDate",
        )
        if name in names
    ]
    dates = date_candidates or ["__retrieved_at"]
    discriminator = next(
        (
            name
            for name in (
                "TRADE_ID",
                "ITEM_CODE",
                "HOLDER_CODE",
                "PERSON_CODE",
                "BOND_COMBINE_CODE",
                "FUND_CODE",
                "BOARD_CODE",
                "ORG_CODE",
                "TYPE_CODE",
            )
            if name in names
        ),
        None,
    )
    has_override = dataset["dataset_id"] in KEY_FIELD_OVERRIDES
    primary = list(KEY_FIELD_OVERRIDES.get(dataset["dataset_id"], (company, dates[0])))
    if not has_override and discriminator and discriminator not in primary:
        primary.append(discriminator)
    missing = [field for field in primary if field not in names]
    if missing:
        raise ValueError(
            f"{dataset['dataset_id']}: primary_key_fields缺少响应字段: {missing}"
        )
    invalid = [field for field in primary if field.startswith("__")]
    if invalid:
        raise ValueError(
            f"{dataset['dataset_id']}: primary_key_fields不得包含合成字段: {invalid}"
        )
    return primary, dates


def _build_datasets(plan: dict[str, Any]) -> dict[str, Any]:
    datasets = []
    for source in plan["datasets"]:
        history_mode, enumeration, pagination, update = _history(source)
        primary_key_fields, date_fields = _record_contract(source)
        is_sdk = source["provider"] == "baostock"
        datasets.append(
            {
                "dataset_id": source["dataset_id"],
                "plan_group_id": source["plan_group_id"],
                "provider": source["provider"],
                "upstream_identity": source["provider"],
                "request": _request_contract(source),
                "history_mode": history_mode,
                "history_enumeration": enumeration,
                "primary_key_fields": primary_key_fields,
                "date_fields": date_fields,
                "pagination": pagination,
                "update_category": update,
                "empty_result": {
                    "success_evidence": [
                        "SDK success code and exhausted result set" if is_sdk else "protocol success and required response shape"
                    ],
                    "empty_evidence": [
                        "success code with zero exhausted rows" if is_sdk else "valid empty result contract including Eastmoney 9201 where registered"
                    ],
                    "does_not_prove": ["company has no historical record", "fact is not applicable", "fact was not disclosed"],
                },
                "expected_field_count": source["field_count"],
                "field_coverage_status": source["sample_status"],
                "applicability": "resolved company, market, report companyType and research category must match the registered protocol",
                "sample_evidence_hashes": [sample["response_sha256"] for sample in source["samples"]],
            }
        )
    payload = _envelope("datasets", "structured_data_datasets")
    payload["datasets"] = datasets
    return payload


def _nature(plan: dict[str, Any], dataset: dict[str, Any], raw_name: str) -> tuple[str, str]:
    exceptions = plan["field_class_exceptions"]
    dataset_id = dataset["dataset_id"]
    exception_group = {
        "company_basic": "company_basic",
        "pledge": "pledge",
        "capital_projects": "capital_projects",
        "surveys": "surveys",
        "management_roster": "management_roster",
        "staff_pay": "staff_pay",
        "float_holders_history": "float_holders_history",
        "forecasts": "forecasts",
        "tags": "tags",
    }.get(dataset_id)
    if exception_group:
        group = exceptions[exception_group]
        value = group.get(raw_name, group.get("*"))
        mapping = {
            "forecast_or_opinion": "forecast",
            "platform_label_or_reason": "platform_label",
        }
        if value:
            return mapping.get(value, value), f"planning exception {exception_group}.{raw_name}"
    if (dataset["plan_group_id"], raw_name) in KNOWN_FIELD_REFS:
        if dataset["plan_group_id"] in {"B02", "B03", "B04", "B05", "B06", "B07", "F05"}:
            return "provider_defined_indicator", "field plan or design exact reference"
        return "observed", "field plan or design exact reference"
    return "unclassified", "preserved observed position pending semantic classification"


def _period(dataset: dict[str, Any], raw_name: str) -> str:
    group = dataset["plan_group_id"]
    upper = raw_name.upper()
    if "DATE" in upper or upper in {"YEAR", "F86"} or raw_name == "calendar_date":
        return "date_or_period_locator"
    if group in {"F01", "G02", "G03", "G04", "G05", "M01"}:
        return "point_in_time"
    if group in {"F02", "F03"}:
        return "cumulative_or_annual"
    if group == "F04":
        return "single_quarter"
    if group == "B01":
        return "daily"
    if group in {"B02", "B03", "B04", "B05", "B06", "B07"}:
        return "provider_report_period_definition"
    return "record_effective_period"


def _unit_status(raw_name: str, confirmed: bool, observed_types: list[str]) -> str:
    upper = raw_name.upper()
    if any(token in upper for token in ("DATE", "NAME", "CODE", "TYPE", "STATE", "STATUS", "CONTENT", "PROFILE", "REMARK")):
        return "not_applicable"
    if confirmed and "number" in observed_types:
        return "confirmed"
    return "unknown"


def _build_fields(plan: dict[str, Any]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    for dataset in plan["datasets"]:
        for source_field in dataset["fields"]:
            raw_name = source_field["name"]
            ref = (dataset["plan_group_id"], raw_name)
            candidate = dataset["plan_group_id"] == "F05" and raw_name in F05_CANDIDATE_FIELDS
            confirmed = ref in KNOWN_FIELD_REFS and not candidate
            nature, basis = _nature(plan, dataset, raw_name)
            unit_status = _unit_status(raw_name, confirmed, source_field["observed_json_types"])
            formula_eligible = (
                confirmed
                and "number" in source_field["observed_json_types"]
                and nature in {"observed", "provider_defined_indicator"}
                and unit_status == "confirmed"
            )
            fields.append(
                {
                    "field_id": f"{dataset['dataset_id']}.{raw_name}",
                    "dataset_id": dataset["dataset_id"],
                    "plan_group_id": dataset["plan_group_id"],
                    "raw_name": raw_name,
                    "observed_json_types": source_field["observed_json_types"],
                    "nonempty_in_sample": source_field["nonempty_in_sample"],
                    "nature": nature,
                    "definition_status": "candidate" if candidate else "confirmed" if confirmed else "unknown",
                    "unit_status": unit_status,
                    "period_semantics": _period(dataset, raw_name),
                    "scope_semantics": "supplier response field in its registered dataset and company/report scope",
                    "formula_eligible": formula_eligible,
                    "classification_basis": basis,
                }
            )
            if formula_eligible:
                routes.append(
                    {
                        "route_id": f"ROUTE.{dataset['dataset_id']}.{raw_name}.primary",
                        "standard_field_id": f"structured.{dataset['dataset_id']}.{raw_name}",
                        "period_semantics": _period(dataset, raw_name),
                        "consolidation_scope": "registered_supplier_scope",
                        "business_dimension": "registered_raw_dimension",
                        "definition_version": VERSION,
                        "dataset_id": dataset["dataset_id"],
                        "raw_name": raw_name,
                        "role": "primary",
                        "validation_status": "validated",
                    }
                )
    payload = _envelope("fields", "structured_data_fields")
    payload.update(
        {
            "declared_field_positions": len(fields),
            "classification_summary": {
                "total": len(fields),
                "unclassified_nature": sum(item["nature"] == "unclassified" for item in fields),
                "definition_unknown": sum(item["definition_status"] == "unknown" for item in fields),
                "definition_candidate": sum(item["definition_status"] == "candidate" for item in fields),
                "formula_eligible": sum(item["formula_eligible"] for item in fields),
            },
            "fields": fields,
            "routes": routes,
            "dynamic_field_policy": {
                "preserve_raw_response": True,
                "register_unknown_fields": True,
                "default_nature": "unclassified",
                "default_definition_status": "unknown",
                "formula_eligible": False,
            },
        }
    )
    return payload


def _build_peer_sets() -> dict[str, Any]:
    companies = [
        ("贵州茅台", "600519.SH", "sh.600519", "target", "已有业务采集与重要报告基线，作为本轮目标"),
        ("五粮液", "000858.SZ", "sz.000858", "core_peer", "主要酒类业务，可比较产品、渠道、利润与现金回报"),
        ("泸州老窖", "000568.SZ", "sz.000568", "core_peer", "酒类及中高档酒业务，可比较产品结构、渠道与现金回报"),
        ("山西汾酒", "600809.SH", "sh.600809", "operating_peer", "酒类业务，可比较扩张、周转、渠道和利润变化"),
        ("洋河股份", "002304.SZ", "sz.002304", "operating_peer", "以白酒为主，可比较渠道、存货和资本回报"),
        ("古井贡酒", "000596.SZ", "sz.000596", "operating_peer", "以白酒为主，保留酒店等其他业务及子公司差异"),
        ("今世缘", "603369.SH", "sh.603369", "operating_peer", "以白酒为主，可比较分档产品、渠道、区域业务与成长"),
    ]
    payload = _envelope("peer_sets", "structured_data_peer_sets")
    payload["peer_sets"] = [
        {
            "peer_set_id": "peer_set.baijiu_selected.v1",
            "selected_at": "2026-09-08",
            "industry_taxonomy_version": "structured_industry_profiles@1.0.0",
            "decision_kind": "rule_selected",
            "user_confirmed": False,
            "companies": [
                {
                    "company_name": name,
                    "canonical_ticker": ticker,
                    "supplier_security_code": ticker[:6],
                    "baostock_code": bs_code,
                    "role": role,
                    "inclusion_basis": basis,
                }
                for name, ticker, bs_code, role, basis in companies
            ],
            "acquisition_scope": "all_applicable_datasets_all_available_history",
            "display_windows_do_not_limit_acquisition": True,
        }
    ]
    return payload


def _build_schedules(dataset_payload: dict[str, Any]) -> dict[str, Any]:
    rules = {
        "financial": "new_report_catalog_period_or_supplier_period; no daily historical refetch",
        "market": "trading_day 19:00 Asia/Shanghai; keep actual quote date when delayed",
        "event": "daily 20:30 Asia/Shanghai; 30-day overlap and open lifecycle refresh",
        "snapshot": "report or related event plus weekly supplier-summary check",
        "on_demand": "activate only for a concrete research category, then follow publication cadence",
    }
    schedules = []
    for dataset in dataset_payload["datasets"]:
        update = dataset["update_category"]
        item = {
            "dataset_id": dataset["dataset_id"],
            "baseline_scope": dataset["history_mode"],
            "incremental_rule": rules[update],
            "cron_local": "0 19 * * 1-5" if update == "market" else "30 20 * * *" if update == "event" else None,
            "overlap_days": 30 if update == "event" else None,
            "refresh_open_lifecycle": update == "event",
        }
        schedules.append(item)
    payload = _envelope("schedules", "structured_data_schedules")
    payload.update(
        {
            "timezone": "Asia/Shanghai",
            "source_min_interval_seconds": {"eastmoney": 3, "baostock": 3, "cninfo": 5},
            "max_attempts_per_execution": 2,
            "analysis_default_complete_years": 5,
            "analysis_default_published_quarters": 12,
            "analysis_window_limits_acquisition": False,
            "dataset_schedules": schedules,
        }
    )
    return payload


def _build_reading_rules() -> dict[str, Any]:
    rows = [
        ("R01", "quantitative", "同口径单季营收/归母/扣非同比绝对值>=20%，或同比增速环比变化绝对值>=15个百分点；盈亏切换直接触发", ["income_quarter.TOTAL_OPERATE_INCOME", "income_quarter.PARENT_NETPROFIT", "income_quarter.DEDUCT_PARENT_NETPROFIT"], ["RD02", "RD10"]),
        ("R02", "quantitative", "毛利率同比变化绝对值>=3个百分点，或销售/管理/研发费用率同比变化绝对值>=2个百分点", ["baostock_profit.gpMargin", "income_quarter.OPERATE_INCOME", "income_quarter.OPERATE_COST", "income_quarter.SALE_EXPENSE", "income_quarter.MANAGE_EXPENSE", "income_quarter.RESEARCH_EXPENSE"], ["RD02", "RD04"]),
        ("R03", "quantitative", "正归母净利下经营现金流转负，或同口径经营现金流/净利润<0.8且同比下降>=0.2", ["cashflow_quarter.NETCASH_OPERATE", "income_quarter.NETPROFIT", "baostock_cash_flow.CFOToNP"], ["RD05", "RD06"]),
        ("R04", "quantitative", "应收或存货同比增速超过营收同比>=20个百分点，或周转天数同比上升>=20%且增加>=30天", ["balance_fields.ACCOUNTS_RECE", "balance_fields.INVENTORY", "baostock_operation.NRTurnDays", "baostock_operation.INVTurnDays"], ["RD02", "RD03", "RD05"]),
        ("R05", "quantitative", "合同负债同比下降>=20%，或完整有息负债/总资产同比增加>=5个百分点", ["balance_fields.CONTRACT_LIAB", "balance_fields.TOTAL_ASSETS", "K05"], ["RD05", "RD06"]),
        ("R06", "quantitative", "新增正额商誉减值，或资产/信用减值损失达到上期末正归母净资产1%", ["goodwill.GOODWILL_CHANGE", "income_fields.ASSET_IMPAIRMENT_LOSS", "income_fields.CREDIT_IMPAIRMENT_LOSS", "balance_fields.TOTAL_PARENT_EQUITY"], ["RD05"]),
        ("R07", "event", "实控人、董事长、总经理、财务负责人、审计机构或审计意见类型变化", ["controller", "management_roster", "company_basic.ACCOUNT_FIRM", "balance_fields.OPINION_TYPE"], ["RD07", "RD12"]),
        ("R08", "event", "并购、处置、融资或项目金额达到上期末正归母净资产5%，或重大事项影响当前问题", ["seo", "allotment", "bond_issuance", "capital_projects", "capital_raise"], ["RD08"]),
        ("R09", "event", "新担保或诉讼/处罚达到上期末正归母净资产1%，或关键主体/资质事件", ["guarantee", "litigation", "violation", "balance_fields.TOTAL_PARENT_EQUITY"], ["RD07", "RD12"]),
        ("R10", "event", "已实施年度每股现金分红同比下降>=20%，取消分红/回购、用途改变或回购计划股数达到总股本1%", ["dividend", "repurchase", "equity.TOTAL_SHARES"], ["RD08"]),
        ("R11", "correction", "更正关联正在使用的报告/字段且存在异常、实质修订或明确待答问题", ["material_version", "used_field_locator", "open_question"], ["RD05", "RD12"]),
        ("R12", "research_question", "新事件或事实直接影响已有问题、关键假设、跟踪项或结论失效条件", ["research_question_id", "assumption_id", "claim_id"], ["RD10"]),
    ]
    payload = _envelope("reading_rules", "structured_data_reading_rules")
    payload.update(
        {
            "fixed_report_policy": {
                "complete_chinese_annual_report": "required",
                "complete_chinese_interim_report": "required",
                "annual_summary": "catalog_only_unless_question_triggered",
                "english_annual_report": "body_excluded",
                "standalone_audit_pdf": "body_excluded",
            },
            "rules": [
                {
                    "rule_id": rule_id,
                    "trigger_kind": kind,
                    "expression": expression,
                    "input_refs": inputs,
                    "target_routes": targets,
                    "creates_risk_rating": False,
                }
                for rule_id, kind, expression, inputs, targets in rows
            ],
        }
    )
    return payload


METHOD_REFS = {
    "ES01": "docs/methodology/steps/01_business_model.md",
    "ES02": "docs/methodology/steps/02_financial_statements.md",
    "ES03": "docs/methodology/steps/03_governance.md",
    "ES04": "docs/methodology/steps/04_capital_actions.md",
    "ES05": "docs/methodology/steps/05_valuation.md",
    "ES06": "docs/methodology/steps/06_drivers_risks.md",
    "ES07": "docs/methodology/steps/07_industry.md",
    "ES08": "docs/methodology/steps/08_scenarios.md",
}


# Each token is already a physical dataset field/record, an RD/K route, an explicit gap,
# or a separately stored research object. This deliberately does not copy table titles as paths.
QUESTION_SPECS: list[tuple[str, str, tuple[str, ...], str, tuple[str, ...], tuple[str, ...]]] = [
    ("ES01.Q01", "公司是谁、如何盈利？", ("NOW", "BIZ"), "all industries", ("f:company_basic.ORG_CODE", "f:company_basic.ORG_NAME", "f:company_basic.FOUND_DATE", "f:company_basic.MAIN_BUSINESS", "rd:RD01"), ("research:CompanyHistoryRecord",)),
    ("ES01.Q02", "利润由哪些产品、地区和渠道贡献？", ("BIZ",), "actual disclosed segments", ("f:segments.MAINOP_TYPE", "f:segments.ITEM_CODE", "f:segments.ITEM_PARENT_CODE", "f:segments.MAIN_BUSINESS_INCOME", "f:segments.MAIN_BUSINESS_COST", "f:segments.MAIN_BUSINESS_RPOFIT", "rd:RD02", "k:K02"), ("research:SegmentAssetRecord",)),
    ("ES01.Q03", "客户供应商集中度与渠道依赖如何？", ("BIZ",), "businesses with customer or supplier counterparties", ("f:customers_peer.TYPE", "f:customers_peer.AMOUNT", "f:customers_peer.SUM_AMOUNT", "f:customers_peer.TOI_RATIO", "f:customers_peer.RANK", "rd:RD02", "gap:GAP02"), ("research:CounterpartyTenureRecord",)),
    ("ES01.Q04", "量、价、单位成本如何变化？", ("BIZ",), "comparable physical or service units", ("rd:RD03", "f:segments.MAIN_BUSINESS_INCOME", "f:segments.MAIN_BUSINESS_COST", "k:K10", "rd:RD02", "gap:GAP02"), ("research:TerminalPriceRecord",)),
    ("ES01.Q05", "产能、产销存及在建能力如何？", ("BIZ",), "production/resource/utility or capacity constrained services", ("rd:RD03", "f:balance_fields.CIP", "gap:GAP07"), ("research:RampYieldRecord",)),
    ("ES01.Q06", "资本开支处于什么阶段？", ("BIZ", "FIN"), "non-financial or physical-investment segments", ("f:cashflow_fields.CONSTRUCT_LONG_ASSET", "f:balance_fields.CIP", "f:balance_fields.FIXED_ASSET", "f:capital_projects.ITEM_NAME", "f:capital_projects.PLAN_INVEST_AMT", "f:capital_projects.ACTUAL_INPUT_RF", "f:capital_projects.BUILD_PERIOD", "rd:RD03", "gap:GAP05"), ("research:ProjectReturnRecord",)),
    ("ES01.Q07", "研发与人员投入形成哪些可观察产出？", ("BIZ",), "businesses with R&D or technical development", ("f:rd.RESEARCH_EXPENSE", "f:rd.RESEARCH_EXPENSING", "f:rd.RESEARCH_EXPENSE_CAPITALIZATION", "f:rd.RESEARCH_NUM", "f:staff_pay.TOTAL_NUM", "rd:RD04"), ("research:PatentQualityRecord",)),
    ("ES01.Q08", "公司处于怎样的上下游位置？", ("BIZ",), "all industries using corresponding service/funding chain", ("record:customers_peer", "f:subsidiaries.HOLD_ORG_NAME", "f:subsidiaries.HOLD_TYPE", "f:subsidiaries.ORG_HOLD_RATIO", "f:subsidiaries.MAIN_PRODUCTS", "rd:RD01", "rd:RD02"), ("research:ImportDependenceRecord",)),
    ("ES01.Q09", "战略及重大经营变化是什么？", ("NOW", "EVT"), "all industries", ("rd:RD10", "f:capital_projects.ACTUAL_INPUT_RF", "record:subsidiaries"), ("f:surveys.CONTENT", "f:forecasts.RATING")),
    ("ES01.Q10", "技术、成本、渠道、品牌优势有何证据？", ("BIZ",), "all industries, activated per claimed advantage", ("rd:RD10", "record:segments", "record:rd", "rd:RD02", "rd:RD03", "research:ClaimRecord"), ("research:IndependentCustomerEvidence",)),
    ("ES02.Q01", "三表范围和历史是否可比？", ("FIN",), "all industries", ("f:balance_fields.REPORT_DATE", "f:balance_fields.REPORT_TYPE", "f:income_fields.REPORT_DATE", "f:income_fields.REPORT_TYPE", "f:cashflow_fields.REPORT_DATE", "f:cashflow_fields.REPORT_TYPE", "rd:RD05", "gap:GAP01"), ("research:AccountingBridgeRecord",)),
    ("ES02.Q02", "营收和利润怎样增长、哪些非经常项影响？", ("FIN",), "all industries with corresponding income definition", ("f:income_fields.OPERATE_INCOME", "f:income_fields.TOTAL_OPERATE_INCOME", "f:income_fields.PARENT_NETPROFIT", "f:income_fields.DEDUCT_PARENT_NETPROFIT", "f:income_fields.NETPROFIT", "record:income_quarter", "k:K01", "rd:RD05"), ("research:SegmentGrowthRecord",)),
    ("ES02.Q03", "毛利、费用和税负如何变化？", ("FIN",), "non-financial general; industry substitutions apply", ("f:income_fields.OPERATE_COST", "f:income_fields.SALE_EXPENSE", "f:income_fields.MANAGE_EXPENSE", "f:income_fields.RESEARCH_EXPENSE", "f:income_fields.FINANCE_EXPENSE", "f:income_fields.INCOME_TAX", "f:income_fields.TOTAL_PROFIT", "k:K02", "rd:RD05"), ("research:PeerExpenseRateRecord",)),
    ("ES02.Q04", "ROE、杜邦和ROIC由什么驱动？", ("FIN",), "ROE all industries; ROIC where applicable", ("f:baostock_profit.roeAvg", "f:baostock_dupont.dupontROE", "f:baostock_dupont.dupontAssetTurn", "f:baostock_dupont.dupontAssetStoEquity", "f:baostock_dupont.dupontNitogr", "f:em_metrics.ROIC", "k:K03"), ("research:WaccInputRecord",)),
    ("ES02.Q05", "资产质量及减值情况如何？", ("FIN",), "according to actual asset composition", ("f:balance_fields.MONETARYFUNDS", "f:balance_fields.ACCOUNTS_RECE", "f:balance_fields.INVENTORY", "f:balance_fields.CONTRACT_ASSET", "f:balance_fields.GOODWILL", "f:income_fields.CREDIT_IMPAIRMENT_LOSS", "f:income_fields.ASSET_IMPAIRMENT_LOSS", "f:goodwill.GOODWILL_CHANGE", "rd:RD05", "rd:RD06"), ("research:CollateralValuationRecord",)),
    ("ES02.Q06", "债务、到期与偿债能力如何？", ("FIN", "NOW"), "all industries with industry substitutions", ("f:balance_fields.TOTAL_LIABILITIES", "f:balance_fields.TOTAL_ASSETS", "f:balance_fields.TOTAL_CURRENT_LIAB", "k:K05", "rd:RD06", "f:baostock_balance.currentRatio", "f:baostock_balance.quickRatio", "gap:GAP05"), ("research:CreditFacilityRecord",)),
    ("ES02.Q07", "现金流和盈利质量如何？", ("FIN",), "all industries, ratios only where compatible", ("f:cashflow_fields.NETCASH_OPERATE", "f:cashflow_fields.NETCASH_INVEST", "f:cashflow_fields.NETCASH_FINANCE", "f:cashflow_fields.BEGIN_CASH_EQUIVALENTS", "f:cashflow_fields.END_CASH_EQUIVALENTS", "f:income_fields.NETPROFIT", "k:K04", "rd:RD06"), ("research:PeerCashQualityRecord",)),
    ("ES02.Q08", "营运资本占用和自由现金流如何？", ("FIN",), "non-financial general", ("f:balance_fields.ACCOUNTS_RECE", "f:balance_fields.INVENTORY", "f:balance_fields.CONTRACT_ASSET", "f:balance_fields.PREPAYMENT", "f:balance_fields.ACCOUNTS_PAYABLE", "f:balance_fields.CONTRACT_LIAB", "f:cashflow_fields.CONSTRUCT_LONG_ASSET", "f:cashflow_fields.NETCASH_OPERATE", "rd:RD06", "k:K06"), ("research:MaintenanceCapexRecord",)),
    ("ES02.Q09", "每股收益与潜在稀释如何？", ("FIN", "EVT"), "all industries", ("f:income_fields.BASIC_EPS", "f:income_fields.DILUTED_EPS", "f:income_fields.PARENT_NETPROFIT", "f:equity.TOTAL_SHARES", "f:equity.END_DATE", "k:K08", "rd:RD11"), ("research:AntiDilutionTerms",)),
    ("ES02.Q10", "表外责任、会计变化和报表异常有哪些？", ("BIZ", "EVT"), "all industries", ("record:guarantee", "record:litigation", "rd:RD05", "rd:RD06", "f:balance_fields.OPINION_TYPE", "gap:GAP05"), ("research:CorrectionImpactRecord",)),
    ("ES03.Q01", "股权与实际控制关系怎样？", ("NOW", "EVT"), "all industries", ("f:controller.HOLDER_NAME", "f:controller.RELATED_RELATION", "f:controller.HOLD_RATIO", "f:holders_history.HOLDER_CODE", "f:holders_history.HOLD_NUM", "f:holders_history.HOLD_NUM_RATIO", "f:float_holders_history.FREE_HOLDNUM_RATIO", "f:company_basic.REAL_CONTROLER", "f:company_basic.REAL_CONTROLER_CODE", "rd:RD07"), ("record:holder_count",)),
    ("ES03.Q02", "股权质押及控制稳定性有哪些事实？", ("NOW", "EVT"), "all industries after event existence check", ("f:pledge.HOLDER_NAME", "f:pledge.PF_NUM", "f:pledge.PF_HOLD_RATIO", "f:pledge.PF_TSR", "f:pledge.UNFREEZE_STATE", "f:pledge.ACTUAL_UNFREEZE_DATE", "rd:RD07", "record:holders_history"), ("research:PledgeWarningEstimate",)),
    ("ES03.Q03", "人员、任期、薪酬及关键变动如何？", ("NOW", "EVT"), "all industries", ("f:management_roster.PERSON_CODE", "f:management_roster.PERSON_NAME", "f:management_roster.POSITION", "f:management_roster.INCUMBENT_DATE", "f:management_roster.INCUMBENT_TIME", "f:management_salary.SALARY", "f:management_salary.END_DATE", "rd:RD07", "gap:GAP03"), ("research:PeerCompensationRecord",)),
    ("ES03.Q04", "激励与员工持股的约束、摊销及稀释如何？", ("EVT",), "all industries after plan existence check", ("rd:RD11", "k:K08", "record:management_trades", "gap:GAP03"), ("research:ManagementWealthExposure",)),
    ("ES03.Q05", "关联交易和承诺是否履行？", ("EVT", "BIZ"), "all industries", ("rd:RD07", "f:guarantee.IS_RELATED_TRADE", "gap:GAP03"), ("research:CounterpartyPublicRecord",)),
    ("ES03.Q06", "审计、内控与监管整改有哪些记录？", ("BIZ", "EVT"), "all industries", ("f:company_basic.ACCOUNT_FIRM", "f:balance_fields.OPINION_TYPE", "f:balance_fields.OSOPINION_TYPE", "record:litigation", "record:violation", "rd:RD12"), ("research:RemediationProgressRecord",)),
    ("ES04.Q01", "分红政策和实际回报如何？", ("EVT", "FIN"), "all industries", ("f:dividend.PRETAX_BONUS_RMB", "f:dividend.REPORT_DATE", "f:dividend.ASSIGN_PROGRESS", "f:dividend.EQUITY_RECORD_DATE", "f:dividend.EX_DIVIDEND_DATE", "f:dividend.IMPL_PLAN_PROFILE", "rd:RD08", "k:K07"), ("research:DividendCommitmentRecord",)),
    ("ES04.Q02", "回购做了多少、用于什么？", ("EVT",), "all industries after event existence check", ("f:repurchase.REPURPROGRESS", "f:repurchase.REPUROBJECTIVE", "f:repurchase.REPURAMOUNT", "f:repurchase.REPURNUM", "f:repurchase.REPURAMOUNTLIMIT", "f:repurchase.FINISHDATE", "rd:RD08"), ("research:RepurchasePriceComparison",)),
    ("ES04.Q03", "大股东/高管增减持和解禁怎样？", ("EVT",), "all industries", ("f:management_trades.CHANGE_DATE", "f:management_trades.CHANGE_SHARES", "f:management_trades.CHANGE_AMOUNT", "f:management_trades.CHANGE_REASON", "record:holders_history", "f:unlock_peer.FREE_DATE", "f:unlock_peer.CURRENT_FREE_SHARES", "f:unlock_peer.ABLE_FREE_SHARES", "rd:RD08"), ("record:block_trade",)),
    ("ES04.Q04", "融资与募集资金使用完成到哪里？", ("EVT",), "all industries after financing existence check", ("f:seo.ISSUE_NUM", "f:seo.ISSUE_PRICE", "f:seo.TOTAL_RAISE_FUNDS", "record:allotment", "f:capital_raise.FINANCE_TYPE", "f:capital_raise.NET_RAISE_FUNDS", "f:capital_projects.ACTUAL_INPUT_RF", "rd:RD08"), ("research:IssuanceDiscountRecord",)),
    ("ES04.Q05", "并购、出售与商誉形成有哪些责任？", ("EVT",), "all industries after transaction existence check", ("rd:RD08", "record:subsidiaries", "f:balance_fields.GOODWILL", "f:goodwill.GOODWILL_CHANGE", "rd:RD05", "gap:GAP04"), ("research:IntegrationEffectRecord",)),
    ("ES04.Q06", "债券、可转债与资本配置回报如何？", ("EVT", "FIN"), "bond events and non-financial project return", ("f:bond_issuance.BOND_COMBINE_CODE", "f:bond_issuance.ISSUE_SCALE", "f:bond_issuance.ISSUE_COUPON_IR", "f:bond_issuance.EXPIRE_DATE", "f:bond_issuance.INITIAL_TRANSFER_PRICE", "rd:RD08", "rd:RD06", "k:K03", "k:K06", "gap:GAP04"), ("research:AnnouncedProjectYield",)),
    ("ES05.Q01", "当前价格、股本和估值口径是什么？", ("NOW",), "all industries by applicable metric", ("f:baostock_daily.date", "f:baostock_daily.close", "f:baostock_daily.tradestatus", "f:baostock_daily.peTTM", "f:baostock_daily.pbMRQ", "f:baostock_daily.psTTM", "f:baostock_daily.pcfNcfTTM", "f:market_cap.TOTAL_MARKET_CAP", "f:market_cap.NOTLIMITED_MARKETCAP_A", "f:market_cap.TRADE_DATE", "f:equity.TOTAL_SHARES", "record:baostock_adjust", "record:baostock_basic"), ("research:EnterpriseValueSalesRecord",)),
    ("ES05.Q02", "历史估值处于什么位置？", ("FIN",), "all industries by applicable valuation metric", ("record:baostock_daily", "record:baostock_calendar", "k:K09"), ("research:ValuationWindowSensitivity",)),
    ("ES05.Q03", "同行估值是否可比？", ("NOW", "FIN"), "all industries", ("research:PeerSetVersion", "record:baostock_daily", "record:income_fields", "k:K09", "rd:RD01", "rd:RD02"), ("record:forecasts",)),
    ("ES05.Q04", "适用绝对估值模型需要哪些输入？", ("SCN",), "selected industry and operating state", ("k:K05", "k:K06", "k:K11", "research:MethodSpec", "research:AssumptionRecord", "gap:GAP11"), ("research:ReverseDCFRun",)),
    ("ES05.Q05", "EV/EBITDA、分部及行业专用估值需要什么？", ("FIN", "NOW", "SCN"), "only when selected model applies", ("k:K09", "research:IndustryProfileVersion", "research:MethodSpec", "gap:GAP05"), ("research:SotpDiscountAssumption",)),
    ("ES05.Q06", "估值区间对假设与口径多敏感？", ("SCN",), "companies with an applicable model", ("research:ModelRun", "research:AssumptionRecord", "k:K11", "f:baostock_daily.close", "gap:GAP11"), ("record:forecasts",)),
    ("ES06.Q01", "哪些量价、份额或新业务变量驱动增长？", ("BIZ", "IND"), "all industries by activated driver", ("coverage:ES01.actual_inputs", "k:K01", "rd:RD10", "research:IndustryDriverRecord"), ("f:forecasts.RATING", "f:surveys.CONTENT")),
    ("ES06.Q02", "驱动如何传导到利润、现金流和估值？", ("FIN", "SCN"), "all industries", ("research:DriverFact", "research:MethodSpec", "research:AssumptionRecord", "k:K11"), ("research:HistoricalAnalogRecord",)),
    ("ES06.Q03", "当前已发生的风险事件和暴露是什么？", ("NOW", "EVT"), "all industries", ("record:guarantee", "record:litigation", "record:violation", "record:pledge", "coverage:financial_anomalies", "rd:RD10", "rd:RD12"), ("research:CounterpartyRiskRecord",)),
    ("ES06.Q04", "潜在风险、缓释因素和反证是什么？", ("NOW", "SCN"), "all industries", ("rd:RD10", "rd:RD06", "research:ClaimRecord", "research:AssumptionRecord"), ("research:StressTestRun",)),
    ("ES06.Q05", "催化剂、领先指标和失效条件如何跟踪？", ("NOW", "EVT"), "all industries", ("research:TrackingIndicator", "research:ClaimRecord", "research:AssumptionRecord", "record:capital_projects", "record:dividend", "record:repurchase"), ("record:margin", "record:surveys")),
    ("ES07.Q01", "行业边界与公司业务归属是什么？", ("NOW", "BIZ"), "all industries", ("f:company_basic.SWINDUSTRY_CODE2", "f:company_basic.SWINDUSTRY_NAME2", "f:company_basic.CSRC_INDUSTRY_NAME", "record:segments", "research:IndustryProfileVersion"), ("record:tags",)),
    ("ES07.Q02", "行业空间及增速怎样？", ("IND",), "all industries", ("rd:RD09", "k:K01", "gap:GAP06"), ("record:macro_cpi", "record:macro_retail")),
    ("ES07.Q03", "供需、产能投放和库存周期如何？", ("IND",), "industry-specific supply/demand variables", ("research:IndustryProfileVersion", "rd:RD09", "rd:RD03", "gap:GAP07"), ("research:SubstituteSupplyRecord",)),
    ("ES07.Q04", "公司份额和竞争集中度如何？", ("IND",), "industries with a defined market boundary", ("research:IndustryDenominatorRecord", "research:CompanyNumeratorRecord", "research:MarketLeaderRecord", "k:K10", "gap:GAP06"), ("research:PeerSampleShareRecord",)),
    ("ES07.Q05", "价格、成本与产业链议价如何？", ("IND", "BIZ"), "product or service chain specific", ("research:IndustryPriceCostRecord", "rd:RD09", "rd:RD02", "k:K10", "gap:GAP07"), ("research:TerminalResearchRecord",)),
    ("ES07.Q06", "政策、技术替代及同行选择是否变化？", ("NOW", "EVT"), "all industries", ("rd:RD09", "rd:RD10", "research:PeerSetVersion", "research:IndustryProfileVersion", "research:PolicyTechnologyEvent"), ("record:tags", "record:forecasts")),
    ("ES08.Q01", "当前实际基线及关键缺口是什么？", ("NOW", "FIN"), "all industries", ("research:CompanyIdentity", "research:IndustryProfileVersion", "coverage:first_seven_steps", "f:baostock_daily.date", "f:baostock_daily.close"), ("research:LongCycleComparison",)),
    ("ES08.Q02", "悲观、基准、乐观情景采用哪些假设？", ("SCN",), "selected industry model", ("research:AssumptionRecord", "research:AssumptionConfirmationStatus", "k:K11", "gap:GAP11"), ("record:forecasts",)),
    ("ES08.Q03", "情景下盈利、EPS、FCF和估值是多少？", ("SCN",), "selected applicable model only", ("k:K11", "k:K08", "k:K05", "research:IndustryProfileVersion", "research:ModelRun"), ("research:SensitivityRun",)),
    ("ES08.Q04", "哪些证据支持结论与安全边际？", ("SCN",), "all research outputs", ("research:ModelRun", "f:baostock_daily.close", "research:ClaimRecord", "coverage:required_gaps", "research:RatingConfirmationStatus", "gap:GAP11"), ("record:forecasts",)),
    ("ES08.Q05", "后续怎样更新实际值和复核假设？", ("NOW",), "all industries", ("research:TrackingIndicator", "research:AssumptionRecord", "research:ReportVersion", "coverage:ES06.Q05"), ("research:ReminderPreference",)),
]


READING_ROUTES = [
    ("RD01", "中文年报/中报公司业务概要、经营模式；已有招股书公司沿革", ("主体沿革", "产品/服务", "定价结算", "收入确认与收现模式")),
    ("RD02", "管理层讨论中的收入成本、产品/地区/渠道、主要客户供应商", ("类别", "收入", "成本", "利润口径", "采购销售分母", "渠道变化")),
    ("RD03", "经营情况中的产销存、产能、在建工程和项目", ("产量", "销量", "库存", "有效产能", "利用率定义", "项目投产时间")),
    ("RD04", "研发投入、员工、核心技术和研发项目", ("费用化", "资本化", "研发人员", "项目阶段", "可观察产出")),
    ("RD05", "财务附注中的报表基础、合并范围、资产质量、税项和收入确认", ("范围变化", "账龄减值", "有效税率说明", "政策估计变化", "母公司报表")),
    ("RD06", "财务附注中的受限资产、借款、债券、租赁和现金流补充资料", ("可动用现金", "有息债务组成到期", "利率担保", "折旧摊销", "营运资本范围")),
    ("RD07", "公司治理、股东及关联交易、承诺履行和重要事项", ("任离职身份", "实控链", "关联交易金额条款", "承诺期限和履行")),
    ("RD08", "分红回购、融资并购、募集资金使用及资本事项", ("利润年度", "计划实施终止", "实际金额股数", "并购对价承诺", "项目状态")),
    ("RD09", "年报/中报行业经营信息、行业专章及已登记免费行业资料", ("行业规模", "供需", "价格库存", "公司份额分母", "统计范围")),
    ("RD10", "经营讨论中的战略、未来发展、风险因素和触发事件正文", ("驱动约束", "变更日期", "目标时限", "风险暴露", "反证跟踪变量")),
    ("RD11", "股权激励/员工持股公告和股份支付附注", ("授予行权解锁条件", "人数股份", "摊销", "失效完成", "潜在稀释")),
    ("RD12", "完整中文年报中的审计意见、内控评价、监管事项及整改", ("审计内控意见", "缺陷", "整改目标进展", "机构更换和范围")),
]


CALCULATION_ROUTES = [
    ("K01", ("same-scope cumulative flows", "four consecutive quarters", "positive comparable base"), ("single_quarter", "ttm", "yoy_or_profit_switch"), (), "累计相减和TTM仅用于完整同口径流量；零负基数不硬算普通同比"),
    ("K02", ("segment income/cost hierarchy", "same-scope operating income and expenses"), ("segment_mix", "gross_margin", "expense_rates"), (), "父子分类不重复加总；金融收入成本另走行业定义"),
    ("K03", ("defined ROE/ROIC components", "confirmed capital and tax scope"), ("roe_decomposition", "roic"), (), "不同ROE/ROIC口径不混用；WACC保持独立方法输入"),
    ("K04", ("same-scope operating cash flow", "same-scope net profit", "average assets when required"), ("cash_profit_ratio", "accrual_proxy"), (), "非正分母和平均期缺失留缺"),
    ("K05", ("complete interest-bearing debt components", "available cash"), ("interest_bearing_debt", "net_debt"), (), "组成未证明完整时只保存分项，不输出完整净债务"),
    ("K06", ("operating cash flow", "long-term asset cash payment", "NOPAT/depreciation/working-capital when FCFF"), ("cash_flow_proxy", "fcff_when_method_complete"), ("K05",), "现金流代理不得改称FCFF；维护/扩张开支缺分解时留缺"),
    ("K07", ("implemented dividends by profit year", "actual distribution base", "same-year parent profit"), ("cash_dividend_per_share", "payout_ratio"), (), "每十股显式换算；未完成年度和不明分配基数不计算"),
    ("K08", ("reported EPS", "applicable weighted shares", "potential dilution evidence"), ("reported_or_scenario_eps", "dilution_inputs"), (), "期末总股本不得冒充已披露加权EPS分母"),
    ("K09", ("same-date market valuation", "frozen window", "comparable peer metric subset", "complete EV components"), ("valuation_percentile", "peer_comparison", "ev_ebitda_when_complete"), ("K05",), "平值取中秩并保存样本数；亏损PE和定义不符项排除并解释"),
    ("K10", ("same-product quantity and value", "compatible industry denominator"), ("unit_economics", "capacity_utilization", "market_share"), (), "少数同行或社零不得替代行业分母"),
    ("K11", ("method reference", "actual baseline", "confirmed scenario assumptions"), ("earnings", "eps", "fcf", "valuation_range"), ("K05", "K08"), "未确认假设不写入历史实际，不自动赋概率或确认结论"),
]

CALCULATION_INPUT_REFS = {
    "K01": ("f:income_fields.TOTAL_OPERATE_INCOME", "f:income_fields.PARENT_NETPROFIT", "f:income_quarter.TOTAL_OPERATE_INCOME", "f:income_quarter.PARENT_NETPROFIT", "f:cashflow_quarter.NETCASH_OPERATE"),
    "K02": ("f:segments.ITEM_CODE", "f:segments.ITEM_PARENT_CODE", "f:segments.MAIN_BUSINESS_INCOME", "f:segments.MAIN_BUSINESS_COST", "f:income_fields.OPERATE_INCOME", "f:income_fields.OPERATE_COST", "f:income_fields.SALE_EXPENSE", "f:income_fields.MANAGE_EXPENSE", "f:income_fields.RESEARCH_EXPENSE"),
    "K03": ("f:baostock_profit.roeAvg", "f:baostock_dupont.dupontROE", "f:baostock_dupont.dupontAssetTurn", "f:baostock_dupont.dupontAssetStoEquity", "f:em_metrics.ROIC", "research:MethodSpec"),
    "K04": ("f:cashflow_fields.NETCASH_OPERATE", "f:income_fields.NETPROFIT", "f:balance_fields.TOTAL_ASSETS"),
    "K05": ("f:balance_fields.SHORT_LOAN", "f:balance_fields.LONG_LOAN", "f:balance_fields.BOND_PAYABLE", "f:balance_fields.LEASE_LIAB", "f:balance_fields.MONETARYFUNDS", "rd:RD06"),
    "K06": ("f:cashflow_fields.NETCASH_OPERATE", "f:cashflow_fields.CONSTRUCT_LONG_ASSET", "f:cashflow_fields.FA_IR_DEPR", "f:cashflow_fields.IA_AMORTIZE", "rd:RD06", "research:MethodSpec"),
    "K07": ("f:dividend.PRETAX_BONUS_RMB", "f:dividend.REPORT_DATE", "f:dividend.ASSIGN_PROGRESS", "f:income_fields.PARENT_NETPROFIT", "f:equity.TOTAL_SHARES", "rd:RD08"),
    "K08": ("f:income_fields.BASIC_EPS", "f:income_fields.DILUTED_EPS", "f:income_fields.PARENT_NETPROFIT", "f:equity.TOTAL_SHARES", "rd:RD11"),
    "K09": ("f:baostock_daily.peTTM", "f:baostock_daily.pbMRQ", "f:baostock_daily.psTTM", "f:baostock_daily.pcfNcfTTM", "f:market_cap.TOTAL_MARKET_CAP", "f:market_cap.NOTLIMITED_MARKETCAP_A", "f:market_cap.TRADE_DATE", "k:K05", "research:PeerSetVersion"),
    "K10": ("f:segments.MAIN_BUSINESS_INCOME", "f:segments.MAIN_BUSINESS_COST", "rd:RD03", "rd:RD09"),
    "K11": ("research:MethodSpec", "research:AssumptionRecord", "research:ModelRun", "k:K05", "k:K08"),
}


GAPS = [
    ("GAP01", ("母公司三表", "合并范围变化", "政策估计变化"), ("RD05重要报告精读",), "明确母公司表头、期间、单位和字段定位"),
    ("GAP02", ("客户供应商身份金额", "渠道", "产品销量产能单位成本"), ("验证C03同义备选", "RD02/RD03定向精读"), "匿名记录仅满足匿名可答项；身份和量价分别取证"),
    ("GAP03", ("完整离任", "关联交易", "承诺", "激励条件"), ("RD07", "RD11"), "保存主体、有效日期、条款和履行状态"),
    ("GAP04", ("并购对价承诺", "债券余额转股赎回"), ("RD08", "RD06"), "取得对应交易/债券全链状态，发行表不能代替"),
    ("GAP05", ("完整有息债务", "可动用现金", "表外事项", "维护开支", "EV组成"), ("RD05", "RD06", "K05/K06/K09"), "逐项字段完整且公式组成检查通过"),
    ("GAP06", ("行业规模分母", "CR3/CR5完整分子"), ("登记长期免费统计资料", "RD09同口径资料"), "产品、地区、期间、单位和统计范围一致"),
    ("GAP07", ("白酒批价渠道库存", "行业运营量价供需"), ("RD03", "RD09", "单独注册并验证新免费源"), "取得实际披露或有效长期免费记录"),
    ("GAP08", ("银行保险券商F05候选字段", "报表companyType"), ("隔离真实行业样本", "RD05/RD09"), "相应公司类型、非空值、定义单位和历史范围通过"),
    ("GAP09", ("中债利率", "行业免费备选"), ("首次相关任务验证已定位协议",), "窗口、单位、可得边界和有效记录通过"),
    ("GAP10", ("新公司模糊身份", "市场支持", "分类成员", "同行可比性"), ("主数据前置任务", "有界同行筛查"), "唯一证券身份和业务可比证据成立"),
    ("GAP11", ("方法skeleton", "未来参数", "评级确认"), ("独立方法工作", "真实用户确认"), "方法、假设和分析状态分别完成"),
    ("GAP12", ("历史断档", "未知单位定义", "长期免费不可得字段"), ("同义免费补源", "已有重要报告", "新注册来源"), "具体期间字段获得定义和定位；完整登记本身不闭合缺口"),
]


def _path(token: str, *, semantic: str, period: str) -> dict[str, Any]:
    kind, value = token.split(":", 1)
    base = {
        "dataset_id": None,
        "raw_name": None,
        "route_id": None,
        "gap_id": None,
        "research_object_kind": None,
        "semantic_key": semantic,
        "period_semantics": period,
        "scope_semantics": "question company, requested analysis period and registered business dimension",
        "unit_semantics": "registered provider unit or requirement-specific declared unit",
    }
    if kind == "f":
        dataset_id, raw_name = value.split(".", 1)
        base.update(kind="raw_field", dataset_id=dataset_id, raw_name=raw_name, support_status="supported")
    elif kind == "record":
        base.update(kind="record_set", dataset_id=value, support_status="supported")
    elif kind == "rd":
        base.update(kind="reading_section", route_id=value, support_status="reading_required")
    elif kind == "k":
        base.update(kind="calculation", route_id=value, support_status="supported")
    elif kind == "gap":
        base.update(kind="gap", gap_id=value, support_status="unsupported_free_source")
    elif kind == "research":
        base.update(kind="research_context", research_object_kind=value, support_status="research_context_required")
    elif kind == "coverage":
        base.update(kind="coverage_snapshot", route_id=value, support_status="research_context_required")
    else:
        raise ValueError(f"未知需求路径token: {token}")
    return base


def _build_requirements(field_payload: dict[str, Any]) -> dict[str, Any]:
    field_keys = {(item["dataset_id"], item["raw_name"]): item for item in field_payload["fields"]}
    questions: list[dict[str, Any]] = []
    requirements: list[dict[str, Any]] = []
    for question_id, title, periods, applicability, required_tokens, optional_tokens in QUESTION_SPECS:
        step_id = question_id.split(".", 1)[0]
        required_ids: list[str] = []
        optional_ids: list[str] = []
        for requiredness, tokens, target in (
            ("required", required_tokens, required_ids),
            ("optional", optional_tokens, optional_ids),
        ):
            for ordinal, token in enumerate(tokens, start=1):
                suffix = ordinal if requiredness == "required" else 500 + ordinal
                requirement_id = f"REQ.{question_id}.{suffix:03d}"
                path = _path(token, semantic=f"{question_id}:{token}", period="+".join(periods))
                if path["kind"] == "raw_field":
                    field = field_keys.get((path["dataset_id"], path["raw_name"]))
                    if field is None:
                        raise ValueError(f"{question_id}: 未找到原字段{token}")
                    if field["definition_status"] in {"candidate", "unknown"}:
                        path["support_status"] = "candidate_mapping"
                requirements.append(
                    {
                        "requirement_id": requirement_id,
                        "question_id": question_id,
                        "requiredness": requiredness,
                        "combination": "all_of",
                        "paths": [path],
                        "applicability_condition": applicability,
                        "output_slots": [f"answer.{question_id}", f"evidence.{question_id}"],
                        "depends_on_requirement_ids": [],
                    }
                )
                target.append(requirement_id)
        questions.append(
            {
                "question_id": question_id,
                "step_id": step_id,
                "ordinal": int(question_id[-2:]),
                "title": title,
                "method_ref": METHOD_REFS[step_id],
                "period_modes": list(periods),
                "applicability": applicability,
                "required_requirement_ids": required_ids,
                "optional_requirement_ids": optional_ids,
                "output_slots": [f"answer.{question_id}", f"evidence.{question_id}", f"gaps.{question_id}"],
            }
        )

    legacy = json.loads(LEGACY_QUESTIONS_PATH.read_text(encoding="utf-8"))
    payload = _envelope("research_requirements", "eight_step_research_requirements")
    payload.update(
        {
            "question_set_id": "eight_step_research_requirements",
            "question_set_version": VERSION,
            "default_analysis_window": {
                "complete_fiscal_years": 5,
                "published_quarters": 12,
                "business_reports": "five annual plus latest interim and prior comparable interim",
                "events": "five years plus older open lifecycle",
                "industry_months": 60,
                "acquisition_scope_is_independent": True,
            },
            "questions": questions,
            "requirements": requirements,
            "calculation_routes": [
                {
                    "route_id": route_id,
                    "input_semantics": list(inputs),
                    "input_refs": list(CALCULATION_INPUT_REFS[route_id]),
                    "output_semantics": list(outputs),
                    "depends_on_routes": list(dependencies),
                    "formula_boundary": boundary,
                }
                for route_id, inputs, outputs, dependencies, boundary in CALCULATION_ROUTES
            ],
            "reading_routes": [
                {
                    "route_id": route_id,
                    "materials_and_sections": materials,
                    "extraction_targets": list(targets),
                }
                for route_id, materials, targets in READING_ROUTES
            ],
            "gaps": [
                {
                    "gap_id": gap_id,
                    "affected_inputs": list(inputs),
                    "next_paths": list(paths),
                    "completion_evidence": evidence,
                }
                for gap_id, inputs, paths, evidence in GAPS
            ],
            "legacy_crosswalk": {
                "legacy_question_set_id": "business_model_questions",
                "legacy_question_set_version": legacy["version"],
                "legacy_file_sha256": hashlib.sha256(LEGACY_QUESTIONS_PATH.read_bytes()).hexdigest(),
                "mappings": {
                    f"ES01.Q{ordinal:02d}": legacy["topics"][ordinal - 1]["question_id"]
                    for ordinal in range(1, 11)
                },
            },
        }
    )
    return payload


def _industry_input(
    profile_id: str,
    input_id: str,
    token: str,
    activation: str,
    question_ids: tuple[str, ...],
    *,
    support_status: str | None = None,
    replaces: tuple[str, ...] = (),
) -> dict[str, Any]:
    path = _path(
        token,
        semantic=f"industry.{profile_id}.{input_id}",
        period="industry-compatible comparison period",
    )
    if support_status is not None:
        path["support_status"] = support_status
    return {
        "input_id": f"IND.{profile_id}.{input_id}",
        "path": path,
        "activation_condition": activation,
        "affected_question_ids": list(question_ids),
        "replaces_semantic_keys": list(replaces),
    }


def _profile(
    profile_id: str,
    label: str,
    inputs: list[dict[str, Any]],
    *,
    mode: str = "industry",
    activation_evidence: tuple[str, ...],
    conditional_groups: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "label": label,
        "mode": mode,
        "inherits_common_requirements": True,
        "activation_evidence": list(activation_evidence),
        "unknown_behavior": "pending_not_general_fallback",
        "inputs": inputs,
        "conditional_groups": list(conditional_groups),
        "preserve_unreplaced_steps": True,
    }


def _build_industry_profiles() -> dict[str, Any]:
    general = [
        _industry_input("general", "balance", "record:balance_fields", "confirmed ordinary industrial/commercial reporting type", ("ES02.Q01", "ES02.Q05", "ES02.Q06")),
        _industry_input("general", "income", "record:income_fields", "confirmed ordinary industrial/commercial reporting type", ("ES02.Q02", "ES02.Q03")),
        _industry_input("general", "cashflow", "record:cashflow_fields", "confirmed ordinary industrial/commercial reporting type", ("ES02.Q07", "ES02.Q08")),
        _industry_input("general", "notes", "rd:RD05", "ordinary industrial/commercial evidence is confirmed", ("ES02.Q01", "ES02.Q05")),
    ]
    manufacturing = [
        _industry_input("manufacturing", "capacity", "rd:RD03", "company operates owned/contracted production with comparable capacity", ("ES01.Q05", "ES01.Q06", "ES07.Q03")),
        _industry_input("manufacturing", "unit_cost", "rd:RD02", "same product and scope quantities exist", ("ES01.Q04", "ES07.Q05")),
        _industry_input("manufacturing", "industry_inputs", "rd:RD09", "manufacturing profile active", ("ES07.Q02", "ES07.Q03", "ES07.Q05")),
        _industry_input("manufacturing", "operating_gap", "gap:GAP07", "capacity/order/utilization variables are not evidenced", ("ES01.Q05", "ES07.Q03")),
    ]
    consumer = [
        _industry_input("consumer", "segments", "record:segments", "consumer products/services and comparable segments are evidenced", ("ES01.Q02", "ES01.Q04", "ES07.Q05")),
        _industry_input("consumer", "counterparties", "record:customers_peer", "distribution or concentrated counterparties apply", ("ES01.Q03", "ES01.Q08")),
        _industry_input("consumer", "volume", "rd:RD03", "physical product volume is applicable", ("ES01.Q04", "ES01.Q05")),
        _industry_input("consumer", "channel", "rd:RD02", "distribution/channel model is evidenced", ("ES01.Q03", "ES01.Q10")),
        _industry_input("consumer", "price_inventory_gap", "gap:GAP07", "industry price or channel inventory is required", ("ES07.Q03", "ES07.Q05")),
    ]
    technology = [
        _industry_input("technology", "rd", "record:rd", "technology profile active", ("ES01.Q07", "ES07.Q06")),
        _industry_input("technology", "commercialization", "rd:RD04", "technology project evidence exists", ("ES01.Q07", "ES07.Q06")),
        _industry_input("technology", "hardware_capacity", "rd:RD03", "hardware production is evidenced", ("ES01.Q04", "ES01.Q05", "ES07.Q03")),
        _industry_input("technology", "hardware_yield_gap", "gap:GAP07", "hardware yield/operating volume is required", ("ES01.Q04", "ES07.Q03")),
        _industry_input("technology", "software_subscription", "rd:RD01", "subscription/software service model is evidenced", ("ES01.Q01", "ES01.Q04")),
        _industry_input("technology", "software_retention", "gap:GAP12", "ARR/retention/customer counts are required and not evidenced", ("ES01.Q04", "ES07.Q03")),
    ]
    bank = [
        _industry_input("bank", "nim", "f:em_metrics.NET_INTEREST_MARGIN", "bank reporting type and definition are validated", ("ES02.Q03",), support_status="candidate_mapping", replaces=("general.gross_margin",)),
        _industry_input("bank", "npl", "f:em_metrics.NON_PERFORMING_LOAN", "bank reporting type and definition are validated", ("ES02.Q05",), support_status="candidate_mapping", replaces=("general.inventory_quality",)),
        _industry_input("bank", "capital", "f:em_metrics.NEWCAPITALADER", "bank reporting type and definition are validated", ("ES02.Q06",), support_status="candidate_mapping", replaces=("general.current_ratio",)),
        _industry_input("bank", "liquidity", "f:em_metrics.LIQUIDITY_COVERAGE_RATIO", "bank reporting type and definition are validated", ("ES02.Q06",), support_status="candidate_mapping", replaces=("general.quick_ratio",)),
        _industry_input("bank", "definitions", "rd:RD09", "bank profile active", ("ES02.Q03", "ES02.Q05", "ES02.Q06", "ES05.Q04")),
        _industry_input("bank", "sample_gap", "gap:GAP08", "candidate fields lack companyType/nonempty/unit evidence", ("ES02.Q03", "ES02.Q05", "ES02.Q06")),
    ]
    insurance = [
        _industry_input("insurance", "embedded_value", "f:em_metrics.NHJZ_CURRENT_AMT", "life insurance business and field definition are validated", ("ES05.Q05",), support_status="candidate_mapping"),
        _industry_input("insurance", "life_nbv", "f:em_metrics.NBV_LIFE", "life insurance business and field definition are validated", ("ES02.Q03", "ES05.Q05"), support_status="candidate_mapping"),
        _industry_input("insurance", "life_nbv_rate", "f:em_metrics.NBV_RATE", "life insurance business and field definition are validated", ("ES02.Q03",), support_status="candidate_mapping"),
        _industry_input("insurance", "solvency", "f:em_metrics.SOLVENCY_AR", "insurance reporting type and definition are validated", ("ES02.Q06",), support_status="candidate_mapping", replaces=("general.current_ratio", "general.quick_ratio")),
        _industry_input("insurance", "property_casualty", "rd:RD09", "property/casualty business is evidenced", ("ES02.Q03", "ES02.Q05")),
        _industry_input("insurance", "sample_gap", "gap:GAP08", "insurance candidate fields or P&C ratios lack evidence", ("ES02.Q03", "ES02.Q05", "ES02.Q06", "ES05.Q05")),
    ]
    securities = [
        _industry_input("securities", "risk_coverage", "f:em_metrics.RISK_COVERAGE", "securities reporting type and definition are validated", ("ES02.Q06",), support_status="candidate_mapping"),
        _industry_input("securities", "net_capital", "f:em_metrics.NET_CAPITAL_LIABILITIES", "securities reporting type and definition are validated", ("ES02.Q06",), support_status="candidate_mapping"),
        _industry_input("securities", "fee_income", "f:income_fields.FEE_COMMISSION_INCOME", "securities income statement definition is validated", ("ES01.Q02", "ES02.Q03")),
        _industry_input("securities", "segments", "rd:RD09", "brokerage/investment banking/asset management/proprietary segment applies", ("ES01.Q02", "ES07.Q03")),
        _industry_input("securities", "sample_gap", "gap:GAP08", "regulatory ratios or segment rates lack evidence", ("ES02.Q03", "ES02.Q06", "ES05.Q03")),
    ]
    real_estate = [
        _industry_input("real_estate", "contract_liability", "f:balance_fields.CONTRACT_LIAB", "real-estate development business is evidenced", ("ES02.Q05", "ES02.Q08")),
        _industry_input("real_estate", "projects", "rd:RD03", "project development business is evidenced", ("ES01.Q05", "ES01.Q06")),
        _industry_input("real_estate", "restricted_cash_debt", "rd:RD06", "real-estate profile active", ("ES02.Q06", "ES05.Q04")),
        _industry_input("real_estate", "nav_gap", "gap:GAP05", "NAV or complete debt inputs are required", ("ES05.Q05",)),
        _industry_input("real_estate", "sales_gap", "gap:GAP07", "contract sales/collections/project saleable value are required", ("ES01.Q04", "ES07.Q03")),
    ]
    resources = [
        _industry_input("resources", "reserves_output", "rd:RD03", "resource extraction business is evidenced", ("ES01.Q04", "ES01.Q05")),
        _industry_input("resources", "prices_costs", "rd:RD09", "specific commodity and region are identified", ("ES07.Q02", "ES07.Q03", "ES07.Q05")),
        _industry_input("resources", "segments", "record:segments", "multiple resources/products exist", ("ES01.Q02",)),
        _industry_input("resources", "commodity_gap", "gap:GAP07", "complete quantity/price/cost source is absent", ("ES01.Q04", "ES07.Q03")),
    ]
    utility = [
        _industry_input("utility", "electricity", "rd:RD03", "electricity generation/supply business is evidenced", ("ES01.Q04", "ES01.Q05", "ES07.Q03")),
        _industry_input("utility", "water", "rd:RD03", "water treatment/supply business is evidenced", ("ES01.Q04", "ES01.Q05", "ES07.Q03")),
        _industry_input("utility", "gas", "rd:RD03", "gas distribution business is evidenced", ("ES01.Q04", "ES01.Q05", "ES07.Q03")),
        _industry_input("utility", "tariff_cost", "rd:RD09", "applicable utility segment is identified", ("ES07.Q05",)),
        _industry_input("utility", "operating_gap", "gap:GAP07", "industry quantity/tariff mechanism lacks evidence", ("ES07.Q03", "ES07.Q05")),
    ]
    preprofit = [
        _industry_input("preprofit", "cash", "f:balance_fields.MONETARYFUNDS", "company is not yet sustainably profitable", ("ES02.Q06", "ES05.Q04")),
        _industry_input("preprofit", "operating_cash", "f:cashflow_fields.NETCASH_OPERATE", "company is not yet sustainably profitable", ("ES02.Q07", "ES08.Q03")),
        _industry_input("preprofit", "capex", "f:cashflow_fields.CONSTRUCT_LONG_ASSET", "company is not yet sustainably profitable", ("ES01.Q06", "ES08.Q03")),
        _industry_input("preprofit", "shares", "f:equity.TOTAL_SHARES", "company is not yet sustainably profitable", ("ES02.Q09", "ES08.Q03")),
        _industry_input("preprofit", "milestones", "rd:RD10", "company is not yet sustainably profitable", ("ES01.Q09", "ES06.Q05")),
        _industry_input("preprofit", "runway_assumption", "research:AssumptionRecord", "cash runway model is selected", ("ES05.Q04", "ES08.Q02"), support_status="research_context_required"),
    ]

    profiles = [
        _profile("general", "普通工商", general, mode="base", activation_evidence=("ordinary companyType", "two-period segment evidence where classification is needed")),
        _profile("manufacturing", "制造", manufacturing, activation_evidence=("C01 classification", "C02 principal business", "report companyType")),
        _profile("consumer", "消费", consumer, activation_evidence=("C01 classification", "C02 consumer principal business and share"), conditional_groups=(
            {"condition_id": "distribution_chain", "evidence_required": ["distribution/channel business evidence"], "unknown_behavior": "pending", "input_ids": ["IND.consumer.counterparties", "IND.consumer.channel", "IND.consumer.price_inventory_gap"]},
        )),
        _profile("technology", "科技", technology, activation_evidence=("C01 classification", "C02 technology principal business"), conditional_groups=(
            {"condition_id": "hardware", "evidence_required": ["hardware production evidence"], "unknown_behavior": "pending", "input_ids": ["IND.technology.hardware_capacity", "IND.technology.hardware_yield_gap"]},
            {"condition_id": "software_subscription", "evidence_required": ["software/subscription revenue model evidence"], "unknown_behavior": "pending", "input_ids": ["IND.technology.software_subscription", "IND.technology.software_retention"]},
        )),
        _profile("bank", "银行", bank, activation_evidence=("bank report companyType", "bank regulatory/business classification")),
        _profile("insurance", "保险", insurance, activation_evidence=("insurance report companyType", "life/P&C business evidence"), conditional_groups=(
            {"condition_id": "life", "evidence_required": ["life insurance business evidence"], "unknown_behavior": "pending", "input_ids": ["IND.insurance.embedded_value", "IND.insurance.life_nbv", "IND.insurance.life_nbv_rate"]},
            {"condition_id": "property_casualty", "evidence_required": ["property/casualty insurance business evidence"], "unknown_behavior": "pending", "input_ids": ["IND.insurance.property_casualty"]},
        )),
        _profile("securities", "券商", securities, activation_evidence=("securities report companyType", "licensed securities business evidence")),
        _profile("real_estate", "地产", real_estate, activation_evidence=("C01 classification", "C02 development/project principal business")),
        _profile("resources", "资源周期", resources, activation_evidence=("C01 classification", "C02 specific resource principal business")),
        _profile("utility", "公用事业", utility, activation_evidence=("C01 classification", "C02 electricity/water/gas principal business"), conditional_groups=(
            {"condition_id": "electricity", "evidence_required": ["electricity segment evidence"], "unknown_behavior": "pending", "input_ids": ["IND.utility.electricity"]},
            {"condition_id": "water", "evidence_required": ["water segment evidence"], "unknown_behavior": "pending", "input_ids": ["IND.utility.water"]},
            {"condition_id": "gas", "evidence_required": ["gas segment evidence"], "unknown_behavior": "pending", "input_ids": ["IND.utility.gas"]},
        )),
        _profile("preprofit", "尚未盈利", preprofit, mode="overlay", activation_evidence=("period-specific profitability and cash-flow evidence", "research classification evidence")),
    ]
    payload = _envelope("industry_profiles", "structured_data_industry_profiles")
    payload.update(
        {
            "profile_selection_version": VERSION,
            "mixed_business_rule": "连续两个可比年度单一主营分类占比均至少50%方可自动选择单一画像；否则组合已证画像并按分部适用，未知保持待确认",
            "preprofit_is_overlay": True,
            "classification_sources_are_not_research_profiles": True,
            "unknown_industry_default_profile": None,
            "profiles": profiles,
        }
    )
    return payload


def main() -> None:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if plan.get("document_role") != "planning_evidence_not_runtime_config":
        raise ValueError("输入必须是冻结的规划证据，而不是未知运行配置")
    if plan.get("dataset_count") != 55 or plan.get("dataset_field_entries") != 2504:
        raise ValueError("规划证据计数不符合55数据集/2504字段位置基线")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    datasets = _build_datasets(plan)
    fields = _build_fields(plan)
    peer_sets = _build_peer_sets()
    schedules = _build_schedules(datasets)
    reading_rules = _build_reading_rules()
    requirements = _build_requirements(fields)
    industry_profiles = _build_industry_profiles()

    _write("datasets.v1.json", datasets)
    _write("fields.v1.json", fields)
    _write("peer_sets.v1.json", peer_sets)
    _write("schedules.v1.json", schedules)
    _write("reading_rules.v1.json", reading_rules)
    _write("research_requirements.v1.json", requirements)
    _write("industry_profiles.v1.json", industry_profiles)


if __name__ == "__main__":
    main()
