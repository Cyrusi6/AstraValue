from __future__ import annotations

from datetime import datetime, timezone

import pytest

from analysis.models import (
    FactRecord,
    StructuredAcquisitionMethod,
    StructuredFactAdmission,
    StructuredFactNature,
    StructuredQualityStatus,
    VerificationStatus,
)
from analysis.structured.consumption import (
    fact_consumption_failures,
    is_fact_consumable,
    latest_consumable_facts,
)


T1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 1, tzinfo=timezone.utc)
HASH = "a" * 64


def admission(**updates) -> StructuredFactAdmission:
    values = {
        "acquisition_method": StructuredAcquisitionMethod.SUPPLIER_STRUCTURED,
        "nature": StructuredFactNature.OBSERVED,
        "quality": StructuredQualityStatus.PASSED,
        "source_policy_id": "structured-first",
        "source_policy_version": "1",
        "field_definition_id": "F02.OPERATE_INCOME",
        "field_definition_version": "1",
        "raw_resource_snapshot_id": "snapshot:1",
        "snapshot_sha256": HASH,
        "row_key": "600519.SH|2024-12-31",
        "field_path": "result.data[0].OPERATE_INCOME",
        "original_value": "100",
        "original_unit": "元",
        "retrieved_at": T2,
        "available_at": T2,
    }
    values.update(updates)
    return StructuredFactAdmission(**values)


def supplier_fact(**updates) -> FactRecord:
    values = {
        "ticker": "600519",
        "metric_id": "revenue",
        "value": 100.0,
        "unit": "元",
        "as_of": T2,
        "source_ids": ["source:em"],
        "verification_status": VerificationStatus.SUPPLIER_DIRECT,
        "structured_admission": admission(),
    }
    values.update(updates)
    return FactRecord(**values)


def test_supplier_direct_is_computed_from_bound_admission() -> None:
    fact = supplier_fact()
    assert is_fact_consumable(fact)
    assert fact_consumption_failures(fact) == ()

    with pytest.raises(ValueError, match="未通过结构化准入谓词"):
        supplier_fact(
            structured_admission=admission(
                nature=StructuredFactNature.PROVIDER_ESTIMATE,
            )
        )


def test_old_statuses_round_trip_without_reclassification() -> None:
    legacy = FactRecord(
        ticker="600519",
        metric_id="revenue",
        value=90,
        source_ids=["source:official"],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        as_of=T1,
    )
    restored = FactRecord.model_validate_json(legacy.model_dump_json())
    assert restored.verification_status == VerificationStatus.AUTHORITATIVE_SINGLE
    assert restored.structured_admission is None
    assert is_fact_consumable(restored)


def test_current_research_and_strict_historical_availability_are_separate() -> None:
    fact = supplier_fact(as_of=T1)
    assert is_fact_consumable(fact, as_of=T1)
    assert not is_fact_consumable(fact, as_of=T1, strict_historical=True)
    assert "not_available_at_as_of" in fact_consumption_failures(
        fact,
        as_of=T1,
        strict_historical=True,
    )


def test_latest_selector_ignores_unusable_newer_value() -> None:
    old = supplier_fact(fact_id="old", as_of=T1)
    pending = FactRecord(
        fact_id="new",
        ticker="600519",
        metric_id="revenue",
        value=200,
        verification_status=VerificationStatus.PENDING,
        as_of=T2,
    )
    assert latest_consumable_facts([old, pending])["revenue"].fact_id == "old"
