"""Explicit, evidence-pinned reinterpretation; never edits acquisition registries."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .registry import PROJECT_ROOT
from .storage import canonical_sha256

CONTRACT_FILES = {
    "eastmoney-financial-interpretation-v1.0.0": ("eastmoney-financial-interpretation-v1.0.0.json", "622a3a30137617e0969217449ee37ec615a086279c0b2cd3f4e0ff1e5a012499"),
    "baostock-interpretation-v1.0.0": (
        "baostock-interpretation-v1.0.0.json",
        "9521639a413e60c670eb00a892343e7825eaf8f4fb91f93244d0a0f9289e598d"),
}


def load_interpretation(identifier: str, context) -> dict:
    if identifier not in CONTRACT_FILES:
        raise ValueError(f"unknown interpretation contract: {identifier}")
    filename, expected_hash = CONTRACT_FILES[identifier]
    path = PROJECT_ROOT / "config/structured_data/interpretations" / filename
    contract = json.loads(path.read_text(encoding="utf-8"))
    if contract.get("contract_id") != identifier or contract.get("content_sha256") != expected_hash:
        raise ValueError("registered interpretation version/hash mismatch")
    validate_interpretation(contract, context)
    return contract


def validate_interpretation(contract: dict, context) -> None:
    from .materialization import _timestamp, _validate_rule

    payload = {k: v for k, v in contract.items() if k != "content_sha256"}
    if (contract.get("schema") != "structured-interpretation.v1"
            or canonical_sha256(payload) != contract.get("content_sha256")):
        raise ValueError("interpretation contract hash/schema mismatch")
    effective = _timestamp(contract["effective_at"])
    sources = context.frozen_config.get("source_definitions", [])
    compatible = [c for c in contract["compatibility"] if all(
        getattr(context, key) == c[key] for key in
        ("field_registry_id", "field_registry_version", "field_registry_hash"))]
    if not any(any(s.get("source_definition_id") == c["source_definition_id"]
                   and str(s.get("version")) == c["source_definition_version"]
                   and s.get("content_hash") == c["source_content_hash"]
                   and canonical_sha256({k: v for k, v in s.items() if k != "content_hash"}) == s["content_hash"]
                   for s in sources) for c in compatible):
        raise ValueError("interpretation incompatible with frozen source/field registry")
    evidence_path = (PROJECT_ROOT / contract["evidence_file"]).resolve()
    if not evidence_path.is_relative_to(PROJECT_ROOT / "docs/acquisition"):
        raise ValueError("interpretation evidence path outside evidence directory")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if canonical_sha256(evidence) != contract["evidence_sha256"]:
        raise ValueError("interpretation evidence file hash mismatch")
    for doc in evidence["documents"].values():
        if hashlib.sha256(doc["content"].encode("utf-8")).hexdigest() != doc["sha256"]:
            raise ValueError("official documentation bytes hash mismatch")
        if _timestamp(doc["retrieved_at"]) > effective:
            raise ValueError("interpretation predates its documentation evidence")
    for key, rule in contract["rules"].items():
        _validate_rule(key, rule)
        if rule["nature"] != "observed" or not rule.get("evidence"):
            raise ValueError("interpretation requires observed field and evidence")
        if context.field_registry_version not in rule["input_descriptors"]:
            raise ValueError("interpretation input descriptor missing")
        for ref in rule["evidence"]:
            doc = evidence["documents"][ref["document_id"]]
            if ref["sha256"] != doc["sha256"] or ref["url"] != doc["url"] or not ref["locators"]:
                raise ValueError("field documentation reference mismatch")
            lines = doc["content"].splitlines()
            for locator in ref["locators"]:
                if (not 1 <= locator["line"] <= len(lines)
                        or lines[locator["line"]-1] != locator["quote"]):
                    raise ValueError("field documentation locator mismatch")


def interpretation_field_failure(context, job, record, field, rule, row, source, interpretation):
    """Only replace the exact registered descriptive deficiency, never failed quality."""
    if field.get("definition_version") != context.field_registry_version:
        return "definition_version_missing_or_mismatched"
    if (field.get("raw_field_name") not in context.frozen_config.get("known_fields", {}).get(job["dataset_id"], [])
            or field.get("dataset_id") != job["dataset_id"]
            or field.get("record_version_id") != record["record_version_id"]):
        return "field_mapping_or_identity_mismatch"
    if source.get("upstream_identity") != interpretation.get("upstream_identity", "baostock") or not any(
            c["field_registry_hash"] == context.field_registry_hash
            and c["source_definition_id"] == job["source_definition_id"]
            and c["source_definition_version"] == job["source_definition_version"]
            and c["source_content_hash"] == source.get("content_hash")
            for c in interpretation["compatibility"]):
        return "interpretation_source_mismatch"
    descriptor = rule["input_descriptors"][context.field_registry_version]
    if any(field.get(k) != v for k, v in descriptor.items()):
        return "interpretation_input_descriptor_mismatch"
    if not record.get("row_key") or field.get("field_path") != f"$.{rule['raw_field']}":
        return "field_locator_missing_or_mismatched"
    if rule["raw_field"] not in row or row[rule["raw_field"]] != field.get("value"):
        return "original_value_mismatch"
    if rule.get("allowed_values") and field["value"] not in rule["allowed_values"]:
        return "categorical_value_outside_contract"
    if job["dataset_id"] == "baostock_daily":
        query = next((q for q in source["queries"] if q["query_id"] == job["dataset_id"]), {})
        params = query.get("parameter_template", {})
        if (params.get("frequency") != "d" or row.get("adjustflag") != params.get("adjustflag")
                or row.get("adjustflag") not in {"1", "2", "3"}):
            return "market_frequency_or_adjustment_mismatch"
        if row.get("tradestatus") not in {"0", "1"}:
            return "market_trading_status_missing"
    return None


def interpreted_period_end(rule, row, original_end, observed: datetime):
    from .materialization import _business_date
    if original_end is None:
        return None
    if rule["business_date_field"] == "observation_date":
        return observed.date()
    try:
        return _business_date(row[rule["business_date_field"]])
    except (KeyError, ValueError):
        return None


def numeric_semantic_failure(rule, row, numeric):
    from .materialization import _number
    from decimal import DecimalException

    raw = rule["raw_field"]
    if rule["dataset_id"] == "customers_peer" and str(row.get("RANK")) not in {"1", "2", "3", "4", "5"}:
        return "counterparty_remainder_is_not_disclosed_amount"
    if rule["dataset_id"] == "baostock_adjust" and numeric <= 0:
        return "adjustment_factor_not_positive"
    if raw in {"totalShare", "liqaShare"} and (numeric < 0 or numeric != numeric.to_integral_value()):
        return "share_count_invalid"
    if rule["dataset_id"] != "baostock_daily":
        return None
    if raw in {"open", "high", "low", "close", "preclose", "volume", "amount"} and numeric < 0:
        return "market_value_negative"
    try:
        if raw == "pctChg" and _number(row.get("preclose")) == 0:
            return "price_change_zero_denominator"
        if row.get("tradestatus") == "0":
            if raw in {"volume", "amount"} and numeric != 0:
                return "suspended_volume_or_amount_nonzero"
            if raw in {"open", "high", "low", "close"} and numeric != _number(row.get("preclose")):
                return "suspended_price_not_previous_close"
    except (ValueError, DecimalException):
        return "market_semantic_input_missing"
    return None
