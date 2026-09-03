from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from analysis.market_multiples import derive_verified_market_multiples
from analysis.models import FactRecord, SourceRecord, VerificationStatus
from analysis.verification import consolidate_facts


AS_OF = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


def _sources() -> list[SourceRecord]:
    return [
        SourceRecord(
            source_id="official",
            name="正式披露",
            source_type="official-document",
            upstream_source_id="official-document:fixture",
            authority_level=1,
        ),
        SourceRecord(
            source_id="sina",
            name="新浪财务报表",
            source_type="public-adapter",
            upstream_source_id="sina-financial-statements",
            authority_level=4,
        ),
        SourceRecord(
            source_id="eastmoney-market",
            name="东方财富行情",
            source_type="public-adapter",
            upstream_source_id="eastmoney-market",
            authority_level=4,
        ),
        SourceRecord(
            source_id="baostock-market",
            name="BaoStock行情与比率",
            source_type="public-adapter",
            upstream_source_id="baostock-market",
            authority_level=4,
        ),
    ]


def _fact(
    fact_id: str,
    metric_id: str,
    value: float,
    unit: str,
    period_end: date,
    period_type: str,
    source_id: str,
) -> FactRecord:
    return FactRecord(
        fact_id=fact_id,
        ticker="600519",
        metric_id=metric_id,
        value=value,
        unit=unit,
        period_start=(
            date(period_end.year, 1, 1)
            if period_type in {"annual", "cumulative"}
            else None
        ),
        period_end=period_end,
        period_type=period_type,
        as_of=AS_OF,
        source_ids=[source_id],
    )


def _provider_facts(*, external_pe: float | None = None) -> list[FactRecord]:
    facts = [
        _fact(
            "price-eastmoney",
            "market_price",
            20,
            "元",
            date(2026, 9, 2),
            "market_quote",
            "eastmoney-market",
        ),
        _fact(
            "price-baostock",
            "market_price",
            20,
            "元",
            date(2026, 9, 2),
            "market_quote",
            "baostock-market",
        ),
    ]
    financial_values = (
        ("shares_outstanding", 10, "股", date(2026, 6, 30), "instant"),
        ("total_parent_equity", 200, "元", date(2026, 6, 30), "instant"),
        ("net_income_parent", 45, "元", date(2026, 6, 30), "cumulative"),
        ("net_income_parent", 80, "元", date(2025, 12, 31), "annual"),
        ("net_income_parent", 35, "元", date(2025, 6, 30), "cumulative"),
    )
    for metric, value, unit, period_end, period_type in financial_values:
        for source_id in ("official", "sina"):
            facts.append(
                _fact(
                    f"{metric}-{period_end}-{source_id}",
                    metric,
                    value,
                    unit,
                    period_end,
                    period_type,
                    source_id,
                )
            )
    expected_pe = 20 * 10 / (45 + 80 - 35)
    facts.extend(
        [
            _fact(
                "pe-baostock",
                "pe_ttm",
                expected_pe if external_pe is None else external_pe,
                "x",
                date(2026, 9, 2),
                "market_quote",
                "baostock-market",
            ),
            _fact(
                "pb-baostock",
                "pb",
                1,
                "x",
                date(2026, 9, 2),
                "market_quote",
                "baostock-market",
            ),
        ]
    )
    return facts


def _run_two_pass(raw: list[FactRecord]):
    sources = _sources()
    first, _ = consolidate_facts(raw, sources)
    derived = derive_verified_market_multiples(first, raw, sources)
    final, records = consolidate_facts([*raw, *derived.facts], sources)
    return derived, final, records


def test_verified_inputs_deterministically_calculate_and_verify_pe_pb():
    raw = _provider_facts()
    derived, final, _ = _run_two_pass(raw)

    assert derived.warnings == []
    raw_pe = next(item for item in derived.facts if item.metric_id == "pe_ttm")
    raw_pb = next(item for item in derived.facts if item.metric_id == "pb")
    assert raw_pe.value == pytest.approx(20 * 10 / 90)
    assert raw_pb.value == pytest.approx(1)
    assert raw_pe.metadata["ttm_parent_net_income"] == 90
    assert raw_pe.method_ref == "VAL.RELATIVE@1.0.0"
    assert raw_pe.metadata["market_price_value_fact_id"] == "price-eastmoney"
    assert "baostock-market" not in raw_pe.source_ids

    pe = next(item for item in final if item.metric_id == "pe_ttm")
    pb = next(item for item in final if item.metric_id == "pb")
    assert pe.verification_status == VerificationStatus.DUAL_SOURCE
    assert pb.verification_status == VerificationStatus.DUAL_SOURCE
    final_ids = {item.fact_id for item in final}
    assert set(pe.derived_from_fact_ids) <= final_ids
    assert len(pe.derived_from_fact_ids) == 5
    assert len(pb.derived_from_fact_ids) == 3


def test_external_multiple_conflict_is_pending_and_never_averaged():
    expected = 20 * 10 / 90
    derived, final, records = _run_two_pass(_provider_facts(external_pe=9.0))
    formula_pe = next(item for item in derived.facts if item.metric_id == "pe_ttm")
    pe = next(item for item in final if item.metric_id == "pe_ttm")

    assert formula_pe.value == pytest.approx(expected)
    assert pe.value == pytest.approx(expected)
    assert pe.value != pytest.approx((expected + 9.0) / 2)
    assert pe.verification_status == VerificationStatus.PENDING
    pe_records = [
        item
        for item in records
        if {item.left_fact_id, item.right_fact_id}
        == {formula_pe.fact_id, "pe-baostock"}
    ]
    assert pe_records and pe_records[0].accepted_value is None


def test_market_multiples_stop_when_a_required_input_is_not_dual_source():
    raw = [
        item
        for item in _provider_facts()
        if not (
            item.metric_id == "shares_outstanding" and item.source_ids == ["sina"]
        )
    ]
    sources = _sources()
    first, _ = consolidate_facts(raw, sources)
    result = derive_verified_market_multiples(first, raw, sources)

    assert result.facts == []
    assert any("期末总股本" in warning for warning in result.warnings)
