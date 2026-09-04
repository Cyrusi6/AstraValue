from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from ..canonical import canonical_sha256
from ..models import (
    ClaimObjectType,
    CompletenessStatus,
    DirectionKind,
    ExtractionStatus,
)
from .loader import ManifestBoundArtifact


class TableContractError(ValueError):
    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class EvidenceLocatorDraft:
    page: int | None = None
    table_id: str | None = None
    row_label: str | None = None
    column_label: str | None = None
    paragraph_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    excerpt: str | None = None

    @property
    def structurally_complete(self) -> bool:
        if (self.char_start is None) != (self.char_end is None):
            return False
        if self.char_start is not None and self.char_end is not None:
            if self.char_start < 0 or self.char_end <= self.char_start:
                return False
        return any(
            (
                self.page is not None,
                self.table_id is not None,
                self.paragraph_id is not None,
                self.char_start is not None,
            )
        )

    @property
    def field_level_complete(self) -> bool:
        if not self.structurally_complete:
            return False
        if self.paragraph_id is not None or self.char_start is not None:
            return True
        return bool(self.table_id and self.row_label and self.column_label)


@dataclass(frozen=True, slots=True)
class ClaimFieldDraft:
    field_name: str
    predicate: str
    object_type: ClaimObjectType
    object_value: object
    locator: EvidenceLocatorDraft | None
    unit: str | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class RecordDraft:
    company_id: str
    question_id: str
    record_type: str
    record_key: str
    subject_type: str
    subject_id: str
    values: Mapping[str, object]
    fields: tuple[ClaimFieldDraft, ...]
    extraction_status: ExtractionStatus
    completeness_status: CompletenessStatus
    issue_codes: tuple[str, ...] = ()
    validation_metadata: Mapping[str, object] = MappingProxyType({})
    lineage_overrides: Mapping[str, object] = MappingProxyType({})

    @property
    def logical_key(self) -> tuple[object, ...]:
        return (
            self.company_id,
            self.question_id,
            self.record_type,
            self.record_key,
            self.values.get("reference_at"),
            self.values.get("effective_at"),
        )


@dataclass(frozen=True, slots=True)
class TitleRecall:
    title: str
    matched_terms: tuple[str, ...]
    artifact_id: str
    fact_claimed: bool = False


@dataclass(frozen=True, slots=True)
class DeterministicExtraction:
    record_drafts: tuple[RecordDraft, ...]
    title_recalls: tuple[TitleRecall, ...]
    issue_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _TableSpec:
    question_id: str
    required: tuple[str, ...]
    claim_fields: tuple[str, ...]


_ALIASES: dict[str, str] = {
    "person": "governance_person",
    "biography": "biography_claim",
    "roster": "roster",
    "role": "role_tenure",
    "tenure": "role_tenure",
    "ownership": "ownership_position",
    "control": "control_relation",
    "pledge": "pledge_position_snapshot",
    "compensation": "compensation_record",
    "related_party": "related_party_transaction",
    "related_party_relation": "related_party_relation",
    "related_party_transaction": "related_party_transaction",
    "incentive": "incentive_plan",
    "grant": "incentive_grant",
    "vesting": "vesting_condition",
    "auditor": "auditor_engagement",
    "signing_auditor": "auditor_engagement",
    "audit_opinion": "audit_opinion_record",
    "internal_control": "internal_control_record",
    "regulatory": "regulatory_matter",
    "inquiry": "inquiry_record",
    "litigation": "litigation_matter",
    "commitment": "commitment_record",
    "policy": "governance_policy_version",
    "correction": "correction_record",
}


_SPECS: dict[str, _TableSpec] = {
    "governance_person": _TableSpec(
        "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",
        ("person_id", "stable_local_key", "canonical_name"),
        ("canonical_name",),
    ),
    "biography_claim": _TableSpec(
        "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",
        ("person_id", "biography_text"),
        ("biography_text", "period_start", "period_end"),
    ),
    "roster_snapshot": _TableSpec(
        "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",
        ("body_type", "member_ids", "is_complete"),
        ("member_ids",),
    ),
    "role_tenure": _TableSpec(
        "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",
        ("person_id", "role_code", "original_role_text", "role_scope", "acting", "valid_from"),
        ("role_code", "valid_from", "valid_to", "acting"),
    ),
    "ownership_position": _TableSpec(
        "GOV.Q01.OWNERSHIP_CONTROL",
        ("holder_entity_id", "holder_name", "share_class"),
        ("holder_name", "share_count", "ratio", "ratio_basis", "share_class"),
    ),
    "ownership_snapshot": _TableSpec(
        "GOV.Q01.OWNERSHIP_CONTROL",
        ("positions", "is_complete", "reference_at"),
        ("total_share_basis", "capital_basis"),
    ),
    "control_relation": _TableSpec(
        "GOV.Q01.OWNERSHIP_CONTROL",
        ("controller_entity_id", "controlled_entity_id", "relation_type", "direction", "chain_path"),
        ("controller_entity_id", "controlled_entity_id", "relation_type"),
    ),
    "pledge_position_snapshot": _TableSpec(
        "GOV.Q02.PLEDGE_FREEZE",
        ("pledgor_entity_id", "is_complete", "reference_at"),
        ("pledgor_entity_id", "pledged_shares", "pledged_ratio", "ratio_basis", "capital_basis"),
    ),
    "compensation_record": _TableSpec(
        "GOV.Q05.REMUNERATION_INCENTIVES",
        ("person_id", "fiscal_year", "currency", "scope"),
        ("cash_compensation", "equity_component", "fiscal_year"),
    ),
    "related_party_relation": _TableSpec(
        "GOV.Q06.RELATED_PARTIES",
        ("related_entity_id", "relation_type", "basis"),
        ("related_entity_id", "relation_type", "basis"),
    ),
    "related_party_transaction": _TableSpec(
        "GOV.Q06.RELATED_PARTIES",
        ("counterparty_entity_id", "transaction_type", "approval_status", "fiscal_period_start", "fiscal_period_end"),
        ("counterparty_entity_id", "transaction_type", "amount", "approval_status"),
    ),
    "incentive_plan": _TableSpec(
        "GOV.Q05.REMUNERATION_INCENTIVES",
        ("plan_id", "instrument", "status"),
        ("instrument", "grant_pool", "status", "dilution_basis"),
    ),
    "incentive_grant": _TableSpec(
        "GOV.Q05.REMUNERATION_INCENTIVES",
        ("plan_id", "recipient_scope", "quantity", "grant_at"),
        ("recipient_scope", "quantity", "price", "grant_at", "vesting_start_at"),
    ),
    "vesting_condition": _TableSpec(
        "GOV.Q05.REMUNERATION_INCENTIVES",
        ("plan_id", "period_label", "metric", "threshold", "status"),
        ("period_label", "metric", "threshold", "actual_disclosure", "status"),
    ),
    "auditor_engagement": _TableSpec(
        "GOV.Q07.EXTERNAL_AUDIT",
        ("audit_firm_entity_id", "fiscal_period_start", "fiscal_period_end"),
        ("audit_firm_entity_id", "signing_auditors", "fee", "change_reason"),
    ),
    "audit_opinion_record": _TableSpec(
        "GOV.Q07.EXTERNAL_AUDIT",
        ("report_period_start", "report_period_end", "opinion_type"),
        ("opinion_type", "emphasis_or_key_matter"),
    ),
    "internal_control_record": _TableSpec(
        "GOV.Q08.INTERNAL_CONTROL_CORRECTIONS",
        ("report_period_start", "report_period_end", "opinion"),
        ("opinion", "defect_category", "rectification_status"),
    ),
    "regulatory_matter": _TableSpec(
        "GOV.Q09.REGULATORY_DISCLOSURE",
        ("authority", "measure_type", "subject_ids", "decision_date", "disclosed_status"),
        ("authority", "measure_type", "decision_date", "disclosed_status"),
    ),
    "inquiry_record": _TableSpec(
        "GOV.Q09.REGULATORY_DISCLOSURE",
        ("authority", "question_categories", "issued_at", "disclosed_status"),
        ("authority", "question_categories", "issued_at", "responded_at", "disclosed_status"),
    ),
    "litigation_matter": _TableSpec(
        "GOV.Q10.LITIGATION_COMMITMENTS",
        ("disclosed_party_ids", "disclosed_stage", "materiality_basis"),
        ("disclosed_party_ids", "amount", "disclosed_stage", "materiality_basis"),
    ),
    "commitment_record": _TableSpec(
        "GOV.Q10.LITIGATION_COMMITMENTS",
        ("promisor_entity_id", "obligation", "disclosed_fulfillment_status"),
        ("promisor_entity_id", "obligation", "deadline", "disclosed_fulfillment_status"),
    ),
    "governance_policy_version": _TableSpec(
        "GOV.Q11.GOVERNANCE_RULES",
        ("policy_id", "policy_type", "effective_date", "version_hash"),
        ("policy_type", "effective_date", "version_hash"),
    ),
    "correction_record": _TableSpec(
        "GOV.Q08.INTERNAL_CONTROL_CORRECTIONS",
        ("old_value", "new_value", "reason"),
        ("old_value", "new_value", "reason"),
    ),
}


_DECIMAL_FIELDS = frozenset(
    {
        "share_count",
        "ratio",
        "ratio_basis",
        "total_share_basis",
        "pledged_shares",
        "pledged_ratio",
        "cash_compensation",
        "equity_component",
        "amount",
        "grant_pool",
        "dilution_basis",
        "quantity",
        "price",
        "fee",
    }
)
_DATE_FIELDS = frozenset(
    {
        "fiscal_period_start",
        "fiscal_period_end",
        "report_period_start",
        "report_period_end",
        "decision_date",
        "effective_date",
    }
)
_DATETIME_FIELDS = frozenset(
    {
        "reference_at",
        "effective_at",
        "valid_from",
        "valid_to",
        "grant_at",
        "vesting_start_at",
        "issued_at",
        "responded_at",
        "deadline",
        "period_start",
        "period_end",
    }
)
_SORTED_TUPLE_FIELDS = frozenset(
    {
        "member_ids",
        "recipient_person_ids",
        "signing_auditors",
        "subject_ids",
        "question_categories",
        "disclosed_party_ids",
        "source_event_ids",
    }
)
_ORDERED_TUPLE_FIELDS = frozenset({"chain_path"})
_RESERVED_ROW_FIELDS = frozenset(
    {
        "row_key",
        "_key",
        "key",
        "values",
        "locators",
        "columns",
        "units",
        "currencies",
        "validation",
        "_validation",
        "lineage",
        "source_role",
        "upstream_material_id",
        "independence_group",
    }
)
_TITLE_TERMS = (
    "董事",
    "监事",
    "高管",
    "管理层",
    "任免",
    "辞任",
    "质押",
    "监管",
    "问询",
    "诉讼",
    "关联交易",
    "审计",
)


def _freeze_mapping(value: Mapping[str, object] | None) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    return MappingProxyType(dict(value))


def _sequence(value: object, *, field: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TableContractError("table_contract_invalid", f"{field} must be an array")
    return tuple(value)


def _parse_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} is not an ISO datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _parse_date(value: object, *, field: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{field} must be a date, not datetime")
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} is not an ISO date") from exc
    if not isinstance(value, date):
        raise ValueError(f"{field} must be a date")
    return value


def _parse_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError(f"{field} rejects int/float; use a decimal string")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{field} is not a decimal string") from exc
    else:
        raise ValueError(f"{field} must be Decimal or a decimal string")
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _normalize_value(field: str, value: object) -> object:
    if value is None:
        return None
    if field in _DECIMAL_FIELDS:
        return _parse_decimal(value, field=field)
    if field in _DATE_FIELDS:
        return _parse_date(value, field=field)
    if field in _DATETIME_FIELDS:
        return _parse_datetime(value, field=field)
    if field in _SORTED_TUPLE_FIELDS:
        items = _sequence(value, field=field)
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"{field} must contain non-empty strings")
        if len(items) != len(set(items)):
            raise ValueError(f"{field} contains duplicates")
        return tuple(sorted(str(item) for item in items))
    if field in _ORDERED_TUPLE_FIELDS:
        items = _sequence(value, field=field)
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"{field} must contain non-empty strings")
        if len(items) != len(set(items)):
            raise ValueError(f"{field} contains duplicates")
        return tuple(str(item) for item in items)
    if field == "direction":
        return value if isinstance(value, DirectionKind) else DirectionKind(str(value))
    return value


def _normal_values(raw: Mapping[str, object]) -> tuple[dict[str, object], list[str]]:
    values: dict[str, object] = {}
    issues: list[str] = []
    for field, value in raw.items():
        try:
            values[str(field)] = _normalize_value(str(field), value)
        except (TableContractError, TypeError, ValueError) as exc:
            issues.append(f"field_type_invalid:{field}:{exc}")
    return values, issues


def _claim_scalar(value: object) -> tuple[ClaimObjectType, object]:
    if value is None:
        return ClaimObjectType.UNKNOWN, None
    if isinstance(value, bool):
        return ClaimObjectType.BOOLEAN, value
    if isinstance(value, int):
        return ClaimObjectType.INTEGER, value
    if isinstance(value, Decimal):
        return ClaimObjectType.DECIMAL, value
    if isinstance(value, datetime):
        return ClaimObjectType.DATETIME, value
    if isinstance(value, date):
        return ClaimObjectType.DATE, value
    if isinstance(value, str):
        return ClaimObjectType.TEXT, value
    if isinstance(value, DirectionKind):
        return ClaimObjectType.TEXT, value.value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return ClaimObjectType.TEXT, json.dumps(
            list(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return ClaimObjectType.TEXT, str(value)


def _locator(
    raw: object,
    *,
    page: int,
    table_id: str,
    row_label: str,
    fallback_column: str | None,
) -> EvidenceLocatorDraft | None:
    if raw is None and fallback_column is None:
        return None
    if isinstance(raw, str):
        return EvidenceLocatorDraft(
            page=page,
            table_id=table_id,
            row_label=row_label,
            column_label=raw,
        )
    if raw is None:
        return EvidenceLocatorDraft(
            page=page,
            table_id=table_id,
            row_label=row_label,
            column_label=fallback_column,
        )
    if not isinstance(raw, Mapping):
        return None
    return EvidenceLocatorDraft(
        page=raw.get("page", page),
        table_id=raw.get("table_id", table_id),
        row_label=raw.get("row_label", row_label),
        column_label=raw.get("column_label", fallback_column),
        paragraph_id=raw.get("paragraph_id"),
        char_start=raw.get("char_start"),
        char_end=raw.get("char_end"),
        excerpt=raw.get("excerpt"),
    )


def _subject(record_type: str, company_id: str, values: Mapping[str, object]) -> tuple[str, str]:
    field_candidates = {
        "governance_person": ("person_id",),
        "biography_claim": ("person_id",),
        "role_tenure": ("person_id",),
        "compensation_record": ("person_id",),
        "ownership_position": ("holder_entity_id",),
        "control_relation": ("controller_entity_id",),
        "pledge_position_snapshot": ("pledgor_entity_id",),
        "related_party_relation": ("related_entity_id",),
        "related_party_transaction": ("counterparty_entity_id",),
        "regulatory_matter": ("authority",),
        "commitment_record": ("promisor_entity_id",),
    }.get(record_type, ())
    for field in field_candidates:
        value = values.get(field)
        if isinstance(value, str) and value:
            return ("person" if value.startswith("govp:") else "entity"), value
    return "company", company_id


def _lineage(row: Mapping[str, object]) -> Mapping[str, object]:
    nested = row.get("lineage")
    values = dict(nested) if isinstance(nested, Mapping) else {}
    for field in ("source_role", "upstream_material_id", "independence_group"):
        if field in row:
            values[field] = row[field]
    return _freeze_mapping(values)


def _question(table: Mapping[str, object], spec: _TableSpec) -> str:
    value = table.get("question_id", spec.question_id)
    if not isinstance(value, str) or not value:
        raise TableContractError("table_contract_invalid", "question_id is invalid")
    return value


def _table_contract(table: Mapping[str, object]) -> tuple[str, int, str, tuple[object, ...]]:
    table_id = table.get("table_id")
    page = table.get("page")
    version = table.get("schema_version", table.get("version"))
    rows = table.get("rows")
    if not isinstance(table_id, str) or not table_id.strip():
        raise TableContractError("table_contract_invalid", "table_id is required")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise TableContractError("table_contract_invalid", "page must be a positive integer")
    if not isinstance(version, str) or not re.fullmatch(r"1(?:\.\d+(?:\.\d+)?)?", version):
        raise TableContractError(
            "unsupported_table_schema", "only an explicit v1 table schema is supported"
        )
    return table_id, page, version, _sequence(rows, field="rows")


def _row_values(row: Mapping[str, object]) -> Mapping[str, object]:
    nested = row.get("values")
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise TableContractError("table_contract_invalid", "row values must be an object")
        return nested
    return {key: value for key, value in row.items() if key not in _RESERVED_ROW_FIELDS}


def _stable_row_key(row: Mapping[str, object], index: int) -> str:
    value = row.get("row_key", row.get("_key", row.get("key")))
    if not isinstance(value, str) or not value.strip():
        raise TableContractError(
            "row_key_missing", f"row {index} lacks a stable row_key"
        )
    return value.strip()


def _prepare_values(
    record_type: str,
    raw_values: Mapping[str, object],
    table: Mapping[str, object],
) -> tuple[dict[str, object], list[str]]:
    values, issues = _normal_values(raw_values)
    for time_field in ("reference_at", "effective_at", "valid_from", "valid_to"):
        if time_field not in values and time_field in table:
            try:
                values[time_field] = _normalize_value(time_field, table[time_field])
            except (TypeError, ValueError) as exc:
                issues.append(f"field_type_invalid:{time_field}:{exc}")

    if record_type == "incentive_plan" and isinstance(values.get("plan_id"), str):
        if not str(values["plan_id"]).startswith("govplan:"):
            values["plan_id"] = f"govplan:{values['plan_id']}"
    if record_type in {"incentive_grant", "vesting_condition"} and isinstance(
        values.get("plan_id"), str
    ):
        if not str(values["plan_id"]).startswith("govplan:"):
            values["plan_id"] = f"govplan:{values['plan_id']}"
    if record_type == "governance_policy_version" and isinstance(
        values.get("policy_id"), str
    ):
        if not str(values["policy_id"]).startswith("govpolicy:"):
            values["policy_id"] = f"govpolicy:{values['policy_id']}"

    spec = _SPECS[record_type]
    for field in spec.required:
        if field not in values or values[field] is None or values[field] == "":
            issues.append(f"required_field_missing:{field}")

    if record_type == "ownership_position":
        if values.get("share_count") is None and values.get("ratio") is None:
            issues.append("ownership_quantity_missing")
        if values.get("ratio") is not None and values.get("ratio_basis") is None:
            issues.append("ratio_basis_missing")
    if record_type == "pledge_position_snapshot":
        if values.get("pledged_shares") is None or values.get("ratio_basis") is None:
            issues.append("pledge_quantity_or_basis_missing")
        if values.get("pledged_ratio") is not None and values.get("ratio_basis") is None:
            issues.append("ratio_basis_missing")
        if values.get("is_complete") is True and values.get("capital_basis") is None:
            issues.append("capital_basis_missing")
    if record_type == "compensation_record" and (
        values.get("cash_compensation") is None
        and values.get("equity_component") is None
    ):
        issues.append("compensation_amount_missing")
    if record_type in {"related_party_transaction", "litigation_matter"}:
        if (values.get("amount") is None) != (values.get("currency") is None):
            issues.append("amount_currency_pair_invalid")
    if record_type in {"incentive_grant", "auditor_engagement"}:
        amount_field = "price" if record_type == "incentive_grant" else "fee"
        if (values.get(amount_field) is None) != (values.get("currency") is None):
            issues.append("amount_currency_pair_invalid")
    if record_type == "correction_record":
        target_count = sum(
            values.get(name) is not None
            for name in ("corrected_claim_id", "corrected_record_id")
        )
        if target_count != 1:
            issues.append("correction_target_invalid")
        if values.get("old_value") == values.get("new_value"):
            issues.append("correction_value_unchanged")
    return values, issues


def _fields(
    *,
    record_type: str,
    row: Mapping[str, object],
    table: Mapping[str, object],
    values: Mapping[str, object],
    page: int,
    table_id: str,
    row_key: str,
) -> tuple[tuple[ClaimFieldDraft, ...], list[str]]:
    spec = _SPECS[record_type]
    configured = table.get("claim_fields", spec.claim_fields)
    names = tuple(str(item) for item in _sequence(configured, field="claim_fields"))
    locators = row.get("locators", {})
    columns = row.get("columns", table.get("columns", {}))
    units = row.get("units", table.get("units", {}))
    currencies = row.get("currencies", table.get("currencies", {}))
    if not isinstance(locators, Mapping) or not isinstance(columns, Mapping):
        raise TableContractError(
            "table_contract_invalid", "locators and columns must be objects"
        )
    if not isinstance(units, Mapping) or not isinstance(currencies, Mapping):
        raise TableContractError(
            "table_contract_invalid", "units and currencies must be objects"
        )
    predicates = table.get("predicates", {})
    if not isinstance(predicates, Mapping):
        raise TableContractError("table_contract_invalid", "predicates must be an object")

    drafts: list[ClaimFieldDraft] = []
    issues: list[str] = []
    for name in names:
        if name not in values or values[name] is None:
            continue
        locator = _locator(
            locators.get(name),
            page=page,
            table_id=table_id,
            row_label=row_key,
            fallback_column=(str(columns[name]) if name in columns else None),
        )
        if locator is None or not locator.field_level_complete:
            issues.append(f"evidence_locator_missing:{name}")
        object_type, object_value = _claim_scalar(values[name])
        currency = currencies.get(name)
        if currency is None and name in {
            "cash_compensation",
            "equity_component",
            "amount",
            "price",
            "fee",
        }:
            currency = values.get("currency")
        unit = units.get(name)
        if unit is None:
            if name in {"share_count", "pledged_shares", "grant_pool", "quantity"}:
                unit = "share"
            elif name in {"ratio", "pledged_ratio"}:
                unit = "ratio"
        drafts.append(
            ClaimFieldDraft(
                field_name=name,
                predicate=str(predicates.get(name, name)),
                object_type=object_type,
                object_value=object_value,
                locator=locator,
                unit=(str(unit) if unit is not None else None),
                currency=(str(currency) if currency is not None else None),
            )
        )
    if not drafts:
        locator = _locator(
            locators.get("row_key"),
            page=page,
            table_id=table_id,
            row_label=row_key,
            fallback_column=(str(columns["row_key"]) if "row_key" in columns else None),
        )
        if locator is None or not locator.field_level_complete:
            issues.append("evidence_locator_missing:row_key")
        drafts.append(
            ClaimFieldDraft(
                field_name="row_key",
                predicate=f"{record_type}.row",
                object_type=ClaimObjectType.IDENTIFIER,
                object_value=row_key,
                locator=locator,
            )
        )
    return tuple(drafts), issues


def _generic_table(
    table: Mapping[str, object],
    *,
    company_id: str,
) -> tuple[RecordDraft, ...]:
    table_id, page, _version, rows = _table_contract(table)
    kind_value = table.get("record_type") or table.get("kind")
    if kind_value == "governance_table":
        kind_value = table.get("record_type")
    record_type = _ALIASES.get(str(kind_value), str(kind_value))
    if record_type not in _SPECS or record_type == "roster":
        raise TableContractError(
            "unsupported_table_kind", f"unsupported deterministic table kind {kind_value!r}"
        )
    spec = _SPECS[record_type]
    question_id = _question(table, spec)
    drafts: list[RecordDraft] = []
    keys: list[str] = []
    raw_rows: list[tuple[Mapping[str, object], str]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise TableContractError(
                "table_contract_invalid", f"row {index} must be an object"
            )
        key = _stable_row_key(item, index)
        keys.append(key)
        raw_rows.append((item, key))
    duplicate_keys = {key for key in keys if keys.count(key) > 1}

    for row, row_key in raw_rows:
        values, issues = _prepare_values(record_type, _row_values(row), table)
        if row_key in duplicate_keys:
            issues.append("primary_key_duplicate")
        fields, field_issues = _fields(
            record_type=record_type,
            row=row,
            table=table,
            values=values,
            page=page,
            table_id=table_id,
            row_key=row_key,
        )
        issues.extend(field_issues)
        subject_type, subject_id = _subject(record_type, company_id, values)
        validation = {}
        for candidate in (table.get("validation"), row.get("validation"), row.get("_validation")):
            if isinstance(candidate, Mapping):
                validation.update(candidate)
        unique_issues = tuple(dict.fromkeys(issues))
        drafts.append(
            RecordDraft(
                company_id=company_id,
                question_id=question_id,
                record_type=record_type,
                record_key=row_key,
                subject_type=subject_type,
                subject_id=subject_id,
                values=_freeze_mapping(values),
                fields=fields,
                extraction_status=(
                    ExtractionStatus.COMPLETE
                    if not unique_issues
                    else ExtractionStatus.PARTIAL
                ),
                completeness_status=(
                    CompletenessStatus.COMPLETE
                    if not unique_issues
                    else CompletenessStatus.INCOMPLETE
                ),
                issue_codes=unique_issues,
                validation_metadata=_freeze_mapping(validation),
                lineage_overrides=_lineage(row),
            )
        )
    return tuple(drafts)


def _local_person_key(company_id: str, table_id: str, row_key: str) -> str:
    digest = canonical_sha256(
        {"company_id": company_id, "table_id": table_id, "row_key": row_key},
        schema_name="governance-person-local-key",
        schema_version="1",
    )
    return f"det-{digest[:24]}"


def _roster_table(
    table: Mapping[str, object],
    *,
    company_id: str,
    manifest_id: str,
) -> tuple[RecordDraft, ...]:
    table_id, page, _version, rows = _table_contract(table)
    question_id = str(
        table.get("question_id", "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND")
    )
    body_type = str(table.get("body_type", "management"))
    drafts: list[RecordDraft] = []
    member_ids: list[str] = []
    row_keys: list[str] = []

    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise TableContractError(
                "table_contract_invalid", f"roster row {index} must be an object"
            )
        row_key = _stable_row_key(item, index)
        row_keys.append(row_key)
        raw = dict(_row_values(item))
        local_key_value = raw.get("stable_local_key", raw.get("person_key"))
        local_key = (
            str(local_key_value)
            if isinstance(local_key_value, str) and local_key_value.strip()
            else _local_person_key(company_id, table_id, row_key)
        )
        person_id = str(raw.get("person_id", f"govp:{company_id}:{local_key}"))
        canonical_name = raw.get("canonical_name", raw.get("name"))
        person_values = {
            "person_id": person_id,
            "stable_local_key": local_key,
            "canonical_name": canonical_name,
        }
        person_table = {
            **dict(table),
            "kind": "governance_person",
            "record_type": "governance_person",
            "claim_fields": ("canonical_name",),
        }
        person_row = {
            **dict(item),
            "values": person_values,
        }
        person_values_normal, person_issues = _prepare_values(
            "governance_person", person_values, person_table
        )
        person_fields, locator_issues = _fields(
            record_type="governance_person",
            row=person_row,
            table=person_table,
            values=person_values_normal,
            page=page,
            table_id=table_id,
            row_key=row_key,
        )
        person_issues.extend(locator_issues)
        unique = tuple(dict.fromkeys(person_issues))
        drafts.append(
            RecordDraft(
                company_id=company_id,
                question_id=question_id,
                record_type="governance_person",
                record_key=f"person:{row_key}",
                subject_type="person",
                subject_id=person_id,
                values=_freeze_mapping(person_values_normal),
                fields=person_fields,
                extraction_status=(ExtractionStatus.COMPLETE if not unique else ExtractionStatus.PARTIAL),
                completeness_status=(CompletenessStatus.COMPLETE if not unique else CompletenessStatus.INCOMPLETE),
                issue_codes=unique,
                lineage_overrides=_lineage(item),
            )
        )
        member_ids.append(person_id)

        biography = raw.get("biography_text", raw.get("biography"))
        if biography is not None:
            bio_values = {
                "person_id": person_id,
                "biography_text": biography,
                "period_start": raw.get("period_start"),
                "period_end": raw.get("period_end"),
            }
            bio_table = {
                **dict(table),
                "kind": "biography_claim",
                "record_type": "biography_claim",
                "claim_fields": ("biography_text", "period_start", "period_end"),
            }
            bio_row = {**dict(item), "values": bio_values}
            normalized, issues = _prepare_values(
                "biography_claim", bio_values, bio_table
            )
            fields, field_issues = _fields(
                record_type="biography_claim",
                row=bio_row,
                table=bio_table,
                values=normalized,
                page=page,
                table_id=table_id,
                row_key=row_key,
            )
            issues.extend(field_issues)
            unique = tuple(dict.fromkeys(issues))
            drafts.append(
                RecordDraft(
                    company_id=company_id,
                    question_id=question_id,
                    record_type="biography_claim",
                    record_key=f"biography:{row_key}",
                    subject_type="person",
                    subject_id=person_id,
                    values=_freeze_mapping(normalized),
                    fields=fields,
                    extraction_status=(ExtractionStatus.COMPLETE if not unique else ExtractionStatus.PARTIAL),
                    completeness_status=(CompletenessStatus.COMPLETE if not unique else CompletenessStatus.INCOMPLETE),
                    issue_codes=unique,
                    lineage_overrides=_lineage(item),
                )
            )

        roles_value = raw.get("roles")
        if roles_value is None:
            codes = raw.get("role_codes")
            if codes is None and raw.get("role_code") is not None:
                codes = (raw.get("role_code"),)
            roles_value = codes or ()
        if isinstance(roles_value, (str, Mapping)):
            roles = (roles_value,)
        else:
            roles = _sequence(roles_value, field="roles")
        for role_index, role_value in enumerate(roles):
            role = dict(role_value) if isinstance(role_value, Mapping) else {"role_code": role_value}
            role_code = role.get("role_code")
            role_values = {
                "person_id": person_id,
                "role_code": role_code,
                "original_role_text": role.get(
                    "original_role_text", role.get("role_text", role_code)
                ),
                "role_scope": role.get("role_scope", body_type),
                "acting": role.get("acting", raw.get("acting", False)),
                "valid_from": role.get("valid_from", raw.get("valid_from", table.get("valid_from"))),
                "valid_to": role.get("valid_to", raw.get("valid_to")),
                "appointment_event_id": role.get("appointment_event_id"),
                "termination_event_id": role.get("termination_event_id"),
            }
            role_table = {
                **dict(table),
                "kind": "role_tenure",
                "record_type": "role_tenure",
                "claim_fields": ("role_code", "valid_from", "valid_to", "acting"),
            }
            role_locators = dict(item.get("locators", {})) if isinstance(item.get("locators", {}), Mapping) else {}
            if isinstance(role.get("locators"), Mapping):
                role_locators.update(role["locators"])
            role_row = {**dict(item), "values": role_values, "locators": role_locators}
            normalized, issues = _prepare_values(
                "role_tenure", role_values, role_table
            )
            fields, field_issues = _fields(
                record_type="role_tenure",
                row=role_row,
                table=role_table,
                values=normalized,
                page=page,
                table_id=table_id,
                row_key=row_key,
            )
            issues.extend(field_issues)
            unique = tuple(dict.fromkeys(issues))
            drafts.append(
                RecordDraft(
                    company_id=company_id,
                    question_id=question_id,
                    record_type="role_tenure",
                    record_key=f"role:{row_key}:{role_index}",
                    subject_type="person",
                    subject_id=person_id,
                    values=_freeze_mapping(normalized),
                    fields=fields,
                    extraction_status=(ExtractionStatus.COMPLETE if not unique else ExtractionStatus.PARTIAL),
                    completeness_status=(CompletenessStatus.COMPLETE if not unique else CompletenessStatus.INCOMPLETE),
                    issue_codes=unique,
                    lineage_overrides=_lineage(item),
                )
            )

    duplicate_rows = {key for key in row_keys if row_keys.count(key) > 1}
    if duplicate_rows:
        drafts = [
            replace(
                draft,
                extraction_status=ExtractionStatus.PARTIAL,
                completeness_status=CompletenessStatus.INCOMPLETE,
                issue_codes=tuple(
                    dict.fromkeys((*draft.issue_codes, "primary_key_duplicate"))
                ),
            )
            if any(key in draft.record_key for key in duplicate_rows)
            else draft
            for draft in drafts
        ]

    complete = table.get("is_complete") is True and not duplicate_rows
    roster_locator = EvidenceLocatorDraft(page=page, table_id=table_id, excerpt=str(table.get("scope_excerpt")) if table.get("scope_excerpt") else None)
    roster_issues: list[str] = []
    if not member_ids:
        roster_issues.append("required_field_missing:member_ids")
    if table.get("is_complete") is not True:
        roster_issues.append("roster_scope_incomplete")
    roster_values = {
        "body_type": body_type,
        "is_complete": complete,
        "member_ids": tuple(sorted(set(member_ids))),
        "source_manifest_id": manifest_id,
        "reference_at": _normalize_value("reference_at", table["reference_at"])
        if "reference_at" in table
        else None,
    }
    drafts.append(
        RecordDraft(
            company_id=company_id,
            question_id=question_id,
            record_type="roster_snapshot",
            record_key=f"roster:{table_id}",
            subject_type="company",
            subject_id=company_id,
            values=_freeze_mapping(roster_values),
            fields=(
                ClaimFieldDraft(
                    field_name="member_ids",
                    predicate="roster_members",
                    object_type=ClaimObjectType.TEXT,
                    object_value=json.dumps(
                        roster_values["member_ids"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    locator=roster_locator,
                ),
            ),
            extraction_status=(ExtractionStatus.COMPLETE if not roster_issues else ExtractionStatus.PARTIAL),
            completeness_status=(CompletenessStatus.COMPLETE if not roster_issues else CompletenessStatus.INCOMPLETE),
            issue_codes=tuple(roster_issues),
        )
    )
    return tuple(drafts)


class DeterministicTableExtractor:
    """Versioned extractor for normalized, stable governance tables.

    The extractor intentionally does not interpret prose.  A heading/title can
    generate recall metadata only; facts require a structured row and a
    field-level locator.
    """

    extractor_kind = "deterministic"

    def __init__(self, *, name: str = "governance-table", version: str = "1.0.0") -> None:
        if not name.strip() or not version.strip():
            raise ValueError("extractor name and version are required")
        self.name = name
        self.version = version

    def extract_table(
        self,
        table: Mapping[str, object],
        *,
        company_id: str,
        manifest_id: str,
    ) -> tuple[RecordDraft, ...]:
        kind_value = table.get("record_type") or table.get("kind")
        normalized_kind = _ALIASES.get(str(kind_value), str(kind_value))
        if normalized_kind == "roster":
            return _roster_table(
                table, company_id=company_id, manifest_id=manifest_id
            )
        return _generic_table(table, company_id=company_id)

    def extract(
        self,
        artifact: ManifestBoundArtifact,
        *,
        company_id: str,
    ) -> DeterministicExtraction:
        content = artifact.content
        if not isinstance(content, Mapping):
            raise TableContractError(
                "artifact_content_invalid", "deterministic extraction requires an object"
            )
        recalls: list[TitleRecall] = []
        title = content.get("title")
        explicit_matches = content.get("title_matches")
        if isinstance(title, str) and title.strip():
            if explicit_matches is None:
                matches = tuple(term for term in _TITLE_TERMS if term in title)
            elif isinstance(explicit_matches, str):
                matches = (explicit_matches,)
            else:
                matches = tuple(str(item) for item in _sequence(explicit_matches, field="title_matches"))
            if matches:
                recalls.append(
                    TitleRecall(
                        title=title.strip(),
                        matched_terms=tuple(dict.fromkeys(matches)),
                        artifact_id=artifact.artifact_id,
                    )
                )

        tables_value = content.get("tables")
        if tables_value is None:
            tables = (content,) if "rows" in content else ()
        else:
            tables = _sequence(tables_value, field="tables")
        drafts: list[RecordDraft] = []
        for index, table in enumerate(tables):
            if not isinstance(table, Mapping):
                raise TableContractError(
                    "table_contract_invalid", f"table {index} must be an object"
                )
            drafts.extend(
                self.extract_table(
                    table,
                    company_id=company_id,
                    manifest_id=artifact.manifest_id,
                )
            )
        return DeterministicExtraction(
            record_drafts=tuple(drafts),
            title_recalls=tuple(recalls),
        )


# Concise public alias used by callers that do not care about the table detail.
DeterministicExtractor = DeterministicTableExtractor


__all__ = [
    "ClaimFieldDraft",
    "DeterministicExtraction",
    "DeterministicExtractor",
    "DeterministicTableExtractor",
    "EvidenceLocatorDraft",
    "RecordDraft",
    "TableContractError",
    "TitleRecall",
]
