from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from analysis.governance.canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    canonical_sha256,
    load_canonical_json,
    shanghai_date_exclusive_upper_bound,
)


def test_canonical_json_is_utf8_stable_and_sorts_sets_and_keys() -> None:
    left = {
        "中文": "证据",
        "ids": {"b", "a"},
        "at": datetime(2024, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        "amount": Decimal("100.2300"),
    }
    right = {
        "amount": Decimal("100.23"),
        "at": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        "ids": {"a", "b"},
        "中文": "证据",
    }
    expected = (
        '{"amount":"100.23","at":"2024-01-01T00:00:00Z",'
        '"ids":["a","b"],"中文":"证据"}'
    ).encode("utf-8")
    assert canonical_json_bytes(left) == expected
    assert canonical_json_bytes(right) == expected


def test_schema_aware_hash_is_stable_but_versions_are_distinct() -> None:
    payload = {"value": Decimal("0.1")}
    first = canonical_sha256(payload, schema_name="example", schema_version="1")
    assert first == canonical_sha256(
        {"value": Decimal("0.10")}, schema_name="example", schema_version="1"
    )
    assert first != canonical_sha256(
        payload, schema_name="example", schema_version="2"
    )
    assert first != canonical_sha256(
        payload, schema_name="another", schema_version="1"
    )


def test_naive_datetime_and_binary_float_fail_closed() -> None:
    with pytest.raises(CanonicalizationError, match="timezone-aware"):
        canonical_json_bytes({"at": datetime(2024, 1, 1)})
    with pytest.raises(CanonicalizationError, match="use Decimal"):
        canonical_json_bytes({"ratio": 0.1})


def test_shanghai_date_precision_uses_next_local_day_as_upper_bound() -> None:
    assert shanghai_date_exclusive_upper_bound(date(2024, 1, 1)) == datetime(
        2024, 1, 1, 16, 0, tzinfo=timezone.utc
    )


def test_canonical_loader_rejects_valid_but_noncanonical_json() -> None:
    assert load_canonical_json(b'{"a":1,"b":2}') == {"a": 1, "b": 2}
    with pytest.raises(CanonicalizationError, match="not canonical"):
        load_canonical_json(b'{"b": 2, "a": 1}')
