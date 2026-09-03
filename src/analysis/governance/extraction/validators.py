from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..canonical import canonical_sha256
from ..models import (
    ClaimObjectType,
    GovernanceClaim,
    GovernanceEvidenceSpan,
    ValidationCheck,
    ValidationResult,
    VerificationStatus,
)
from .deterministic import RecordDraft


_ISO_4217 = frozenset(
    {
        "AUD",
        "CAD",
        "CHF",
        "CNY",
        "EUR",
        "GBP",
        "HKD",
        "JPY",
        "KRW",
        "MOP",
        "NZD",
        "RUB",
        "SGD",
        "TWD",
        "USD",
    }
)
_MONEY_FIELDS = frozenset(
    {"cash_compensation", "equity_component", "amount", "price", "fee"}
)
_SHARE_FIELDS = frozenset(
    {"share_count", "pledged_shares", "grant_pool", "quantity"}
)
_RATIO_FIELDS = frozenset({"ratio", "pledged_ratio"})


@dataclass(frozen=True, slots=True)
class ValidationAssessment:
    verification_status: VerificationStatus
    checks: tuple[ValidationCheck, ...]

    @property
    def passed(self) -> bool:
        return self.verification_status == VerificationStatus.PASSED


def _check(
    code: str,
    passed: bool,
    detail: str | None = None,
    *,
    span_id: str | None = None,
) -> ValidationCheck:
    return ValidationCheck(
        check_code=code,
        passed=passed,
        detail=detail,
        evidence_span_ids=((span_id,) if span_id is not None else ()),
    )


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return None
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def validate_total_relationship(metadata: Mapping[str, object]) -> tuple[bool, str | None]:
    """Validate an explicitly declared table total without inventing a basis."""

    if not metadata:
        return True, None
    expected = metadata.get("expected_total", metadata.get("reported_total"))
    components = metadata.get("components")
    if components is None:
        return True, None
    if isinstance(components, Mapping):
        values = tuple(components.values())
    elif isinstance(components, Sequence) and not isinstance(
        components, (str, bytes, bytearray)
    ):
        values = tuple(components)
    else:
        return False, "components must be an object or array"
    expected_decimal = _decimal(expected)
    parsed = tuple(_decimal(value) for value in values)
    if expected_decimal is None or any(value is None for value in parsed):
        return False, "total and components must be finite decimal strings"
    tolerance = _decimal(metadata.get("tolerance", "0"))
    if tolerance is None or tolerance < 0:
        return False, "tolerance must be a non-negative decimal string"
    actual = sum((value for value in parsed if value is not None), Decimal("0"))
    if abs(actual - expected_decimal) > tolerance:
        return False, f"component sum {actual} differs from reported total {expected_decimal}"
    return True, None


def validate_period_order(values: Mapping[str, object]) -> tuple[bool, str | None]:
    pairs = (
        ("valid_from", "valid_to"),
        ("fiscal_period_start", "fiscal_period_end"),
        ("report_period_start", "report_period_end"),
        ("period_start", "period_end"),
        ("issued_at", "responded_at"),
        ("grant_at", "vesting_start_at"),
    )
    for start_name, end_name in pairs:
        start = values.get(start_name)
        end = values.get(end_name)
        if start is not None and end is not None:
            if not isinstance(start, (date, datetime)) or not isinstance(
                end, (date, datetime)
            ):
                return False, f"{start_name}/{end_name} are not typed dates"
            if end < start:
                return False, f"{end_name} precedes {start_name}"
    return True, None


class GovernanceFieldValidator:
    """Deterministic field and cross-field checks used before admission."""

    name = "governance-field-validator"
    version = "1.0.0"

    def assess(
        self,
        claim: GovernanceClaim,
        *,
        evidence_span: GovernanceEvidenceSpan | None,
        draft: RecordDraft,
        primary_key_unique: bool = True,
        source_conflict: bool = False,
    ) -> ValidationAssessment:
        checks: list[ValidationCheck] = []
        span_id = evidence_span.evidence_span_id if evidence_span is not None else None

        expected_types: dict[ClaimObjectType, tuple[type[object], ...]] = {
            ClaimObjectType.TEXT: (str,),
            ClaimObjectType.IDENTIFIER: (str,),
            ClaimObjectType.INTEGER: (int,),
            ClaimObjectType.DECIMAL: (Decimal,),
            ClaimObjectType.BOOLEAN: (bool,),
            ClaimObjectType.DATE: (date,),
            ClaimObjectType.DATETIME: (datetime,),
            ClaimObjectType.UNKNOWN: (type(None),),
        }
        typed = isinstance(claim.object_value, expected_types[claim.object_type])
        if claim.object_type == ClaimObjectType.INTEGER and isinstance(
            claim.object_value, bool
        ):
            typed = False
        if claim.object_type == ClaimObjectType.DATE and isinstance(
            claim.object_value, datetime
        ):
            typed = False
        checks.append(_check("field_type_valid", typed, None if typed else "claim value/type mismatch"))

        required_ok = not any(
            code.startswith("required_field_missing:") for code in draft.issue_codes
        )
        checks.append(
            _check(
                "required_fields_present",
                required_ok,
                None if required_ok else "one or more required table fields are absent",
            )
        )

        decimal_ok = True
        if claim.object_type == ClaimObjectType.DECIMAL:
            value = claim.object_value
            decimal_ok = isinstance(value, Decimal) and value.is_finite()
        checks.append(
            _check(
                "decimal_valid",
                decimal_ok,
                None if decimal_ok else "numeric value is not a finite Decimal",
            )
        )

        field_name = next(
            (
                field.field_name
                for field in draft.fields
                if field.predicate == claim.predicate
                and field.object_value == claim.object_value
            ),
            claim.predicate,
        )
        if field_name in _SHARE_FIELDS:
            unit_ok = claim.unit == "share"
        elif field_name in _RATIO_FIELDS:
            unit_ok = claim.unit in {"ratio", "percent"}
        else:
            unit_ok = True
        checks.append(
            _check(
                "unit_valid",
                unit_ok,
                None if unit_ok else f"{field_name} has an invalid or missing unit",
            )
        )

        currency_required = field_name in _MONEY_FIELDS and claim.object_value is not None
        currency_ok = (
            claim.currency in _ISO_4217
            if currency_required
            else claim.currency is None or claim.currency in _ISO_4217
        )
        checks.append(
            _check(
                "currency_iso4217",
                currency_ok,
                None if currency_ok else "currency must be an approved ISO-4217 code",
            )
        )

        time_ok = (
            claim.available_at >= claim.announced_at
            and claim.retrieved_at >= claim.available_at
            and (claim.valid_from is None or claim.valid_to is None or claim.valid_to >= claim.valid_from)
        )
        checks.append(
            _check(
                "date_order_valid",
                time_ok,
                None if time_ok else "claim availability/validity timestamps are reversed",
            )
        )
        period_ok, period_detail = validate_period_order(draft.values)
        checks.append(_check("period_order_valid", period_ok, period_detail))

        key_ok = bool(draft.record_key.strip())
        checks.append(_check("primary_key_present", key_ok, None if key_ok else "record key is empty"))
        checks.append(
            _check(
                "primary_key_unique",
                primary_key_unique,
                None if primary_key_unique else "record key is duplicated in the table",
            )
        )

        total_ok, total_detail = validate_total_relationship(draft.validation_metadata)
        checks.append(_check("total_reconciles", total_ok, total_detail))

        ratio_is_present = any(
            draft.values.get(name) is not None for name in ("ratio", "pledged_ratio")
        )
        basis_ok = not ratio_is_present or draft.values.get("ratio_basis") is not None
        checks.append(
            _check(
                "ratio_basis_present",
                basis_ok,
                None if basis_ok else "a disclosed ratio lacks its calculation basis",
            )
        )

        evidence_ok = bool(
            evidence_span is not None
            and evidence_span.evidence_span_id == claim.evidence_span_id
            and evidence_span.raw_snapshot_id == claim.raw_snapshot_id
            and evidence_span.content_hash == claim.content_hash
            and evidence_span.locator_complete
        )
        checks.append(
            _check(
                "evidence_span_bound",
                evidence_ok,
                None if evidence_ok else "field evidence is absent or bound to different bytes",
                span_id=span_id,
            )
        )

        company_ok = claim.company_id == draft.company_id
        checks.append(_check("company_namespace_valid", company_ok, None if company_ok else "company IDs differ"))
        person_ids: list[str] = []
        if claim.subject_type == "person":
            person_ids.append(claim.subject_id)
        for name in ("person_id",):
            value = draft.values.get(name)
            if isinstance(value, str):
                person_ids.append(value)
        recipients = draft.values.get("recipient_person_ids")
        if isinstance(recipients, Sequence) and not isinstance(
            recipients, (str, bytes, bytearray)
        ):
            person_ids.extend(str(value) for value in recipients)
        person_ok = all(
            value.startswith(f"govp:{draft.company_id}:") for value in person_ids
        )
        checks.append(
            _check(
                "person_namespace_valid",
                person_ok,
                None if person_ok else "a person ID crosses the company-local namespace",
            )
        )

        amount_pairs = (
            ("amount", "currency"),
            ("price", "currency"),
            ("fee", "currency"),
        )
        pair_ok = all(
            (draft.values.get(amount) is None) == (draft.values.get(currency) is None)
            for amount, currency in amount_pairs
            if amount in draft.values
        )
        checks.append(
            _check(
                "amount_currency_pair_valid",
                pair_ok,
                None if pair_ok else "amount and currency must be supplied together",
            )
        )

        finite_nonnegative = True
        for name, value in draft.values.items():
            if name in {
                "share_count",
                "ratio",
                "ratio_basis",
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
            }:
                if isinstance(value, Decimal):
                    if not value.is_finite() or value < 0:
                        finite_nonnegative = False
                elif isinstance(value, float) and (not math.isfinite(value) or value < 0):
                    finite_nonnegative = False
        checks.append(
            _check(
                "domain_nonnegative",
                finite_nonnegative,
                None if finite_nonnegative else "a governance quantity is negative or non-finite",
            )
        )

        draft_ok = not draft.issue_codes
        checks.append(
            _check(
                "extraction_draft_complete",
                draft_ok,
                None if draft_ok else ";".join(draft.issue_codes),
            )
        )
        checks.append(
            _check(
                "source_conflict_absent",
                not source_conflict,
                None if not source_conflict else "independent sources disclose incompatible values",
            )
        )

        if source_conflict:
            status = VerificationStatus.CONFLICTED
        elif all(check.passed for check in checks):
            status = VerificationStatus.PASSED
        else:
            status = VerificationStatus.FAILED
        return ValidationAssessment(status, tuple(checks))

    def validate(
        self,
        claim: GovernanceClaim,
        *,
        evidence_span: GovernanceEvidenceSpan | None,
        draft: RecordDraft,
        validated_at: datetime,
        primary_key_unique: bool = True,
        source_conflict: bool = False,
    ) -> ValidationResult:
        if validated_at.tzinfo is None or validated_at.utcoffset() is None:
            raise ValueError("validated_at must be timezone-aware")
        assessment = self.assess(
            claim,
            evidence_span=evidence_span,
            draft=draft,
            primary_key_unique=primary_key_unique,
            source_conflict=source_conflict,
        )
        identity_payload = {
            "claim_id": claim.claim_id,
            "validator_name": self.name,
            "validator_version": self.version,
            "checks": tuple(check.model_dump(mode="python") for check in assessment.checks),
            "validated_at": validated_at,
        }
        identity = canonical_sha256(
            identity_payload,
            schema_name="governance-validation-result-id",
            schema_version=self.version,
        )
        payload: dict[str, object] = {
            "validation_result_id": f"govvalidation:{identity[:32]}",
            "claim_id": claim.claim_id,
            "validator_name": self.name,
            "validator_version": self.version,
            "verification_status": assessment.verification_status,
            "checks": assessment.checks,
            "validated_at": validated_at,
        }
        return ValidationResult.model_validate_with_hash(payload)


__all__ = [
    "GovernanceFieldValidator",
    "ValidationAssessment",
    "validate_period_order",
    "validate_total_relationship",
]
