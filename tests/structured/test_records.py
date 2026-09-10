from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from analysis.structured.records import (
    DataNature,
    FieldLocator,
    MarketRowState,
    NormalizedValue,
    PeriodKind,
    QualityState,
    RecordCatalogue,
    RecordVersion,
    ValueKind,
    admit_field,
    canonical_json_bytes,
    derive_single_quarter,
    derive_ttm,
    map_market_observation,
    normalize_numeric,
    post_event_available_at,
    select_record_version,
    stable_row_key,
    stable_version_key,
)


UTC = timezone.utc
SNAPSHOT = "a" * 64


def _value(
    fact_id: str,
    value: str,
    period_end: date,
    *,
    period_kind: PeriodKind = PeriodKind.CUMULATIVE,
    value_kind: ValueKind = ValueKind.FLOW,
    scope: str = "consolidated",
) -> NormalizedValue:
    return NormalizedValue(
        fact_id=fact_id,
        field_id="revenue",
        value=Decimal(value),
        unit="CNY",
        currency="CNY",
        period_start=date(period_end.year, 1, 1),
        period_end=period_end,
        period_kind=period_kind,
        value_kind=value_kind,
        scope=scope,
    )


def test_row_identity_is_stable_while_content_version_changes() -> None:
    old = {"SECUCODE": "600519.SH", "REPORT_DATE": "2025-12-31", "VALUE": 1}
    new = {**old, "VALUE": 2}
    old_key = stable_row_key("income_fields", old, ("SECUCODE", "REPORT_DATE"))
    new_key = stable_row_key("income_fields", new, ("SECUCODE", "REPORT_DATE"))
    assert old_key == new_key
    assert stable_version_key(old_key, old, SNAPSHOT) != stable_version_key(new_key, new, SNAPSHOT)


def test_row_key_requires_complete_business_identity() -> None:
    with pytest.raises(ValueError, match="missing"):
        stable_row_key("income_fields", {"SECUCODE": "600519.SH"}, ("SECUCODE", "REPORT_DATE"))


def test_canonical_payload_rejects_nonfinite_values() -> None:
    with pytest.raises(ValueError, match="NaN"):
        canonical_json_bytes({"value": float("nan")})


def test_field_admission_requires_value_definition_unit_and_locator() -> None:
    locator = FieldLocator(SNAPSHOT, "row-1", "$.TOTAL_OPERATE_INCOME")
    ready = admit_field(
        value=100,
        nature=DataNature.OBSERVED,
        definition_id="v1:revenue",
        unit="CNY",
        locator=locator,
    )
    missing = admit_field(
        value=100,
        nature=DataNature.OBSERVED,
        definition_id=None,
        unit="CNY",
        locator=None,
    )
    estimate = admit_field(
        value=100,
        nature=DataNature.PROVIDER_ESTIMATE,
        definition_id="v1:forecast",
        unit="CNY",
        locator=locator,
    )
    assert ready.consumable
    assert not missing.consumable and set(missing.issues) >= {"definition_missing", "locator_missing"}
    assert not estimate.consumable


def test_one_bad_field_does_not_change_another_field_admission() -> None:
    locator = FieldLocator(SNAPSHOT, "row-1", "$.A")
    good = admit_field(value=1, nature=DataNature.OBSERVED, definition_id="A", unit="CNY", locator=locator)
    bad = admit_field(value=2, nature=DataNature.OBSERVED, definition_id=None, unit=None, locator=None)
    assert good.consumable and not bad.consumable


def test_unit_normalization_is_explicit_for_percentages() -> None:
    result = normalize_numeric(
        fact_id="f1",
        field_id="turnover_rate",
        raw_value="12.5",
        raw_unit="percent",
        target_unit="ratio",
        multiplier="0.01",
        period_start=None,
        period_end=date(2025, 12, 31),
        period_kind=PeriodKind.MARKET_QUOTE,
        value_kind=ValueKind.RATIO,
        scope="security",
    )
    assert result.value == Decimal("0.125")


def test_cumulative_to_single_quarter_preserves_negative_result_and_inputs() -> None:
    q1 = _value("q1", "120", date(2025, 3, 31))
    q2 = _value("q2", "100", date(2025, 6, 30))
    derived = derive_single_quarter(q2, q1, fact_id="q2-single", formula_version="cum-diff-v1")
    assert derived.value == Decimal("-20")
    assert derived.period_kind is PeriodKind.SINGLE_QUARTER
    assert [item.fact_id for item in derived.inputs] == ["q2", "q1"]


def test_q1_cumulative_is_single_quarter_without_prior_year_subtraction() -> None:
    q1 = _value("q1", "120", date(2025, 3, 31))
    derived = derive_single_quarter(q1, None, fact_id="q1-single", formula_version="cum-diff-v1")
    assert derived.value == Decimal("120")


@pytest.mark.parametrize(
    "previous",
    [
        None,
        _value("prior-year", "50", date(2024, 3, 31)),
        _value("wrong-scope", "50", date(2025, 3, 31), scope="parent"),
    ],
)
def test_cumulative_conversion_rejects_missing_or_mismatched_inputs(previous) -> None:
    q2 = _value("q2", "100", date(2025, 6, 30))
    with pytest.raises(ValueError):
        derive_single_quarter(q2, previous, fact_id="bad", formula_version="cum-diff-v1")


def test_stock_values_cannot_be_converted_to_single_quarter() -> None:
    stock = _value("assets", "100", date(2025, 6, 30), value_kind=ValueKind.STOCK)
    with pytest.raises(ValueError, match="stock"):
        derive_single_quarter(stock, None, fact_id="bad", formula_version="v1")


def test_ttm_requires_four_consecutive_single_quarter_flows() -> None:
    periods = [date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31), date(2025, 6, 30)]
    quarters = [
        _value(f"q{index}", str(index), period, period_kind=PeriodKind.SINGLE_QUARTER)
        for index, period in enumerate(periods, start=1)
    ]
    result = derive_ttm(quarters, fact_id="ttm", formula_version="ttm-sum-v1")
    assert result.value == Decimal("10")
    assert result.period_kind is PeriodKind.TTM


def test_ttm_rejects_missing_quarter_and_stock_or_ratio() -> None:
    missing = [
        _value("q1", "1", date(2024, 6, 30), period_kind=PeriodKind.SINGLE_QUARTER),
        _value("q2", "1", date(2024, 12, 31), period_kind=PeriodKind.SINGLE_QUARTER),
        _value("q3", "1", date(2025, 3, 31), period_kind=PeriodKind.SINGLE_QUARTER),
        _value("q4", "1", date(2025, 6, 30), period_kind=PeriodKind.SINGLE_QUARTER),
    ]
    with pytest.raises(ValueError, match="consecutive"):
        derive_ttm(missing, fact_id="ttm", formula_version="v1")
    with pytest.raises(ValueError, match="stocks or ratios"):
        derive_ttm(
            [
                _value(f"q{i}", "1", p, period_kind=PeriodKind.SINGLE_QUARTER, value_kind=ValueKind.RATIO)
                for i, p in enumerate(
                    [date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31), date(2025, 6, 30)]
                )
            ],
            fact_id="ttm",
            formula_version="v1",
        )


def _record(value: int, retrieved_day: int, available_day: int, updated_day: int | None) -> RecordVersion:
    row = {"id": "same", "value": value}
    return RecordVersion.create(
        dataset_id="segments",
        raw_row=row,
        key_fields=("id",),
        snapshot_sha256=f"{value:x}" * 64,
        retrieved_at=datetime(2025, 1, retrieved_day, tzinfo=UTC),
        available_at=datetime(2025, 1, available_day, tzinfo=UTC),
        source_updated_at=(datetime(2025, 1, updated_day, tzinfo=UTC) if updated_day else None),
    )


def test_out_of_order_old_revision_does_not_replace_current_version() -> None:
    current = _record(1, 5, 5, 5)
    late_old = _record(2, 10, 10, 4)
    assert current.row_key == late_old.row_key
    assert select_record_version([current, late_old]) is current


def test_strict_point_in_time_excludes_future_available_version() -> None:
    old = _record(1, 5, 5, 5)
    revision = _record(2, 10, 10, 10)
    strict = select_record_version(
        [old, revision], as_of=datetime(2025, 1, 7, tzinfo=UTC), strict_point_in_time=True
    )
    assert strict is old
    assert select_record_version([old, revision]) is revision


def test_record_catalogue_is_append_only_and_preserves_payload_bytes() -> None:
    record = _record(1, 5, 5, 5)
    catalogue = RecordCatalogue()
    assert catalogue.append(record)
    assert not catalogue.append(record)
    original = bytes(record.raw_payload)
    mutable_copy = dict(record.raw_row)
    mutable_copy["value"] = 999
    assert record.raw_payload == original
    assert catalogue.versions("segments", record.row_key) == (record,)


def test_record_selection_rejects_mixed_rows() -> None:
    first = _record(1, 5, 5, 5)
    other = RecordVersion.create(
        dataset_id="segments",
        raw_row={"id": "other", "value": 1},
        key_fields=("id",),
        snapshot_sha256="b" * 64,
        retrieved_at=datetime(2025, 1, 5, tzinfo=UTC),
        available_at=datetime(2025, 1, 5, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="one stable row"):
        select_record_version([first, other])


def test_b_market_rows_distinguish_trading_suspension_nontrading_and_empty_values() -> None:
    retrieved = datetime(2025, 12, 31, 11, tzinfo=UTC)
    traded = map_market_observation(
        {"date": "2025-12-31", "tradestatus": "1", "close": "100.5"},
        calendar_is_open=True,
        retrieved_at=retrieved,
    )
    suspended = map_market_observation(
        {"date": "2025-12-31", "tradestatus": "0", "close": ""},
        calendar_is_open=True,
        retrieved_at=retrieved,
    )
    holiday = map_market_observation(
        {"date": "2026-01-01", "tradestatus": "", "close": None},
        calendar_is_open=False,
        retrieved_at=retrieved,
    )
    empty = map_market_observation(
        {"date": "2025-12-31", "tradestatus": "1", "close": None},
        calendar_is_open=True,
        retrieved_at=retrieved,
    )
    assert traded.state is MarketRowState.TRADED and traded.close == Decimal("100.5")
    assert traded.quote_date == date(2025, 12, 31) and traded.available_at == retrieved
    assert suspended.state is MarketRowState.SUSPENDED and suspended.close is None
    assert holiday.state is MarketRowState.NON_TRADING_DAY and holiday.close is None
    assert empty.state is MarketRowState.SOURCE_EMPTY and empty.close is None
    assert suspended.missing_fields == holiday.missing_fields == empty.missing_fields == ("close",)


def test_b_market_cap_history_selection_respects_trade_date_and_availability() -> None:
    old = RecordVersion.create(
        dataset_id="market_cap",
        raw_row={"SECUCODE": "600519.SH", "TRADE_DATE": "2025-01-05", "TOTAL_MARKET_CAP": 100},
        key_fields=("SECUCODE", "TRADE_DATE"),
        snapshot_sha256="c" * 64,
        retrieved_at=datetime(2025, 1, 5, tzinfo=UTC),
        available_at=datetime(2025, 1, 5, tzinfo=UTC),
    )
    current = RecordVersion.create(
        dataset_id="market_cap",
        raw_row={"SECUCODE": "600519.SH", "TRADE_DATE": "2025-01-10", "TOTAL_MARKET_CAP": 200},
        key_fields=("SECUCODE", "TRADE_DATE"),
        snapshot_sha256="d" * 64,
        retrieved_at=datetime(2025, 1, 10, tzinfo=UTC),
        available_at=datetime(2025, 1, 10, tzinfo=UTC),
    )
    assert old.row_key != current.row_key
    assert select_record_version([old], as_of=datetime(2025, 1, 7, tzinfo=UTC), strict_point_in_time=True) is old
    assert select_record_version([current], as_of=datetime(2025, 1, 7, tzinfo=UTC), strict_point_in_time=True) is None
    assert select_record_version([current]) is current


def test_t_post_event_n_day_result_is_unavailable_until_horizon_and_retrieval() -> None:
    event_at = datetime(2025, 1, 2, 7, tzinfo=UTC)
    horizon_at = datetime(2025, 1, 9, 7, tzinfo=UTC)
    retrieved_at = datetime(2025, 1, 10, 1, tzinfo=UTC)
    available = post_event_available_at(
        event_at=event_at,
        horizon_observed_at=horizon_at,
        retrieved_at=retrieved_at,
    )
    result = RecordVersion.create(
        dataset_id="block_trade",
        raw_row={"TRADE_ID": "T1", "CHANGE_RATE_5DAYS": 12.3},
        key_fields=("TRADE_ID",),
        snapshot_sha256="e" * 64,
        retrieved_at=retrieved_at,
        available_at=available,
    )
    assert select_record_version(
        [result],
        as_of=datetime(2025, 1, 9, 23, tzinfo=UTC),
        strict_point_in_time=True,
    ) is None
    assert select_record_version(
        [result],
        as_of=retrieved_at,
        strict_point_in_time=True,
    ) is result
    with pytest.raises(ValueError, match="cannot precede"):
        post_event_available_at(
            event_at=event_at,
            horizon_observed_at=datetime(2025, 1, 1, tzinfo=UTC),
            retrieved_at=retrieved_at,
        )
