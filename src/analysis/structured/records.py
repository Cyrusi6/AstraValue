from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class DataNature(str, Enum):
    OBSERVED = "observed"
    DETERMINISTIC = "deterministic"
    PROVIDER_ESTIMATE = "provider_estimate"
    FORECAST = "forecast"
    PLATFORM_LABEL = "platform_label"
    SOURCE_TEXT = "source_text"
    ANNOUNCED_PLAN = "announced_plan"
    UNCLASSIFIED = "unclassified"


class QualityState(str, Enum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    DEFINITION_UNKNOWN = "definition_unknown"
    NOT_APPLICABLE = "not_applicable"


class PeriodKind(str, Enum):
    ANNUAL = "annual"
    CUMULATIVE = "cumulative"
    SINGLE_QUARTER = "single_quarter"
    TTM = "ttm"
    INSTANT = "instant"
    MARKET_QUOTE = "market_quote"


class ValueKind(str, Enum):
    FLOW = "flow"
    STOCK = "stock"
    RATIO = "ratio"
    TEXT = "text"
    LABEL = "label"
    FORECAST = "forecast"


class MarketRowState(str, Enum):
    TRADED = "traded"
    SUSPENDED = "suspended"
    NON_TRADING_DAY = "non_trading_day"
    SOURCE_EMPTY = "source_empty"


def _aware_utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _aware_utc(value, name="datetime").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical payload cannot contain NaN or infinity")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MarketObservation:
    quote_date: date
    state: MarketRowState
    close: Decimal | None
    available_at: datetime
    missing_fields: tuple[str, ...]


def map_market_observation(
    row: Mapping[str, Any],
    *,
    calendar_is_open: bool,
    retrieved_at: datetime,
) -> MarketObservation:
    """Preserve quote-day and availability semantics without turning blanks into zero."""

    raw_date = row.get("date")
    if raw_date is None or not str(raw_date).strip():
        raise ValueError("market observation requires its supplier quote date")
    quote_date = date.fromisoformat(str(raw_date).strip())
    available_at = _aware_utc(retrieved_at, name="retrieved_at")
    raw_close = row.get("close")
    close = None if raw_close is None or not str(raw_close).strip() else decimal_value(raw_close)
    missing = tuple(name for name in ("close",) if row.get(name) is None or not str(row.get(name)).strip())
    trade_status = str(row.get("tradestatus") or "").strip()
    if not calendar_is_open:
        state = MarketRowState.NON_TRADING_DAY
    elif trade_status == "0":
        state = MarketRowState.SUSPENDED
    elif close is None:
        state = MarketRowState.SOURCE_EMPTY
    else:
        state = MarketRowState.TRADED
    return MarketObservation(quote_date, state, close, available_at, missing)


def post_event_available_at(
    *,
    event_at: datetime,
    horizon_observed_at: datetime,
    retrieved_at: datetime,
) -> datetime:
    """Admit an N-day outcome only after its actual horizon and retrieval time."""

    event = _aware_utc(event_at, name="event_at")
    horizon = _aware_utc(horizon_observed_at, name="horizon_observed_at")
    retrieved = _aware_utc(retrieved_at, name="retrieved_at")
    if horizon < event:
        raise ValueError("post-event horizon cannot precede the event")
    return max(horizon, retrieved)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: str, *, name: str) -> str:
    normalized = str(value).lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError(f"{name} must be a full SHA-256")
    return normalized


def stable_row_key(
    dataset_id: str,
    row: Mapping[str, Any],
    key_fields: Sequence[str],
    *,
    partition: Mapping[str, Any] | None = None,
) -> str:
    """Build a business row key which is independent of mutable row content."""

    if not dataset_id or not key_fields:
        raise ValueError("dataset_id and at least one row-key field are required")
    missing = [
        name
        for name in key_fields
        if name not in row or row[name] is None or str(row[name]).strip() == ""
    ]
    if missing:
        raise ValueError(f"row-key fields are missing: {', '.join(missing)}")
    identity = {
        "dataset_id": dataset_id,
        "partition": dict(partition or {}),
        "key": [(name, row[name]) for name in key_fields],
    }
    return f"row-{_sha256(canonical_json_bytes(identity))[:32]}"


def stable_version_key(
    row_key: str,
    raw_row: Mapping[str, Any],
    snapshot_sha256: str,
) -> str:
    """Build an immutable content version while keeping the row identity stable."""

    snapshot_sha256 = _require_sha256(snapshot_sha256, name="snapshot_sha256")
    payload = {
        "row_key": row_key,
        "snapshot_sha256": snapshot_sha256,
        "raw_row": raw_row,
    }
    return f"version-{_sha256(canonical_json_bytes(payload))}"


@dataclass(frozen=True, slots=True)
class FieldLocator:
    snapshot_sha256: str
    row_key: str
    field_path: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "snapshot_sha256",
            _require_sha256(self.snapshot_sha256, name="snapshot_sha256"),
        )
        if not self.row_key or not self.field_path:
            raise ValueError("row key and field path are required")


@dataclass(frozen=True, slots=True)
class FieldAdmission:
    value: Any
    nature: DataNature
    quality: QualityState
    definition_id: str | None
    unit: str | None
    locator: FieldLocator | None
    issues: tuple[str, ...] = ()

    @property
    def consumable(self) -> bool:
        return (
            self.value is not None
            and self.nature in {DataNature.OBSERVED, DataNature.DETERMINISTIC}
            and self.quality is QualityState.PASSED
            and bool(self.definition_id)
            and bool(self.unit)
            and self.locator is not None
            and not self.issues
        )


def admit_field(
    *,
    value: Any,
    nature: DataNature,
    definition_id: str | None,
    unit: str | None,
    locator: FieldLocator | None,
    quality: QualityState = QualityState.PASSED,
    issues: Iterable[str] = (),
) -> FieldAdmission:
    """Compute admission from evidence; callers cannot self-assert consumability."""

    normalized_issues = [str(issue) for issue in issues if str(issue).strip()]
    if value is None:
        normalized_issues.append("value_missing")
    if not definition_id:
        normalized_issues.append("definition_missing")
    if not unit:
        normalized_issues.append("unit_missing")
    if locator is None:
        normalized_issues.append("locator_missing")
    if nature not in {DataNature.OBSERVED, DataNature.DETERMINISTIC}:
        normalized_issues.append(f"nature_not_deterministic:{nature.value}")
    effective_quality = quality
    if effective_quality is QualityState.PASSED and normalized_issues:
        effective_quality = QualityState.PARTIAL
    return FieldAdmission(
        value=value,
        nature=nature,
        quality=effective_quality,
        definition_id=definition_id,
        unit=unit,
        locator=locator,
        issues=tuple(dict.fromkeys(normalized_issues)),
    )


def decimal_value(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric observation")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid numeric observation") from exc
    if not parsed.is_finite():
        raise ValueError("numeric observation must be finite")
    return parsed


@dataclass(frozen=True, slots=True)
class FormulaInput:
    fact_id: str
    value: Decimal
    unit: str
    period_end: date

    def __post_init__(self) -> None:
        if not self.fact_id or not self.unit:
            raise ValueError("formula inputs require fact identity and unit")
        object.__setattr__(self, "value", decimal_value(self.value))


@dataclass(frozen=True, slots=True)
class NormalizedValue:
    fact_id: str
    field_id: str
    value: Decimal
    unit: str
    currency: str | None
    period_start: date | None
    period_end: date
    period_kind: PeriodKind
    value_kind: ValueKind
    scope: str
    formula_version: str | None = None
    inputs: tuple[FormulaInput, ...] = ()

    def __post_init__(self) -> None:
        if not self.fact_id or not self.field_id or not self.unit or not self.scope:
            raise ValueError("normalized values require stable identity, unit, and scope")
        object.__setattr__(self, "value", decimal_value(self.value))
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start cannot follow period_end")
        if self.period_kind is PeriodKind.TTM and self.value_kind is not ValueKind.FLOW:
            raise ValueError("TTM is only valid for flow values")
        if self.inputs and not self.formula_version:
            raise ValueError("derived values require a formula version")


def normalize_numeric(
    *,
    fact_id: str,
    field_id: str,
    raw_value: Any,
    raw_unit: str,
    target_unit: str,
    multiplier: Any,
    period_start: date | None,
    period_end: date,
    period_kind: PeriodKind,
    value_kind: ValueKind,
    scope: str,
    currency: str | None = None,
) -> NormalizedValue:
    """Normalize only through an explicit field rule; unit names are never guessed."""

    if not raw_unit or not target_unit:
        raise ValueError("raw and target units must be explicit")
    value = decimal_value(raw_value) * decimal_value(multiplier)
    return NormalizedValue(
        fact_id=fact_id,
        field_id=field_id,
        value=value,
        unit=target_unit,
        currency=currency,
        period_start=period_start,
        period_end=period_end,
        period_kind=period_kind,
        value_kind=value_kind,
        scope=scope,
    )


def _quarter_number(value: date) -> int:
    if (value.month, value.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        raise ValueError("period_end is not a calendar quarter end")
    return value.month // 3


def _quarter_index(value: date) -> int:
    return value.year * 4 + _quarter_number(value) - 1


def derive_single_quarter(
    current: NormalizedValue,
    previous: NormalizedValue | None,
    *,
    fact_id: str,
    formula_version: str,
) -> NormalizedValue:
    if current.value_kind is not ValueKind.FLOW:
        raise ValueError("stock and ratio values cannot be converted to a single quarter")
    if current.period_kind not in {PeriodKind.CUMULATIVE, PeriodKind.ANNUAL}:
        raise ValueError("current input must be a cumulative flow")
    quarter = _quarter_number(current.period_end)
    inputs = [
        FormulaInput(current.fact_id, current.value, current.unit, current.period_end)
    ]
    value = current.value
    if quarter == 1:
        if previous is not None:
            raise ValueError("Q1 cumulative value does not take a previous-quarter input")
    else:
        if previous is None:
            raise ValueError("previous same-year cumulative input is required")
        if previous.period_kind is not PeriodKind.CUMULATIVE:
            raise ValueError("previous input must be cumulative")
        if (
            previous.value_kind is not ValueKind.FLOW
            or previous.field_id != current.field_id
            or previous.unit != current.unit
            or previous.currency != current.currency
            or previous.scope != current.scope
            or previous.period_end.year != current.period_end.year
            or _quarter_number(previous.period_end) != quarter - 1
        ):
            raise ValueError("cumulative inputs must share field, scope, unit, and adjacent year-quarter")
        value -= previous.value
        inputs.append(
            FormulaInput(previous.fact_id, previous.value, previous.unit, previous.period_end)
        )
    return NormalizedValue(
        fact_id=fact_id,
        field_id=current.field_id,
        value=value,
        unit=current.unit,
        currency=current.currency,
        period_start=date(current.period_end.year, current.period_end.month - 2, 1),
        period_end=current.period_end,
        period_kind=PeriodKind.SINGLE_QUARTER,
        value_kind=ValueKind.FLOW,
        scope=current.scope,
        formula_version=formula_version,
        inputs=tuple(inputs),
    )


def derive_ttm(
    quarters: Sequence[NormalizedValue],
    *,
    fact_id: str,
    formula_version: str,
) -> NormalizedValue:
    ordered = sorted(quarters, key=lambda item: item.period_end)
    if len(ordered) != 4:
        raise ValueError("TTM requires exactly four quarters")
    first = ordered[0]
    if any(item.period_kind is not PeriodKind.SINGLE_QUARTER for item in ordered):
        raise ValueError("TTM inputs must be single-quarter values")
    if any(item.value_kind is not ValueKind.FLOW for item in ordered):
        raise ValueError("TTM cannot sum stocks or ratios")
    if any(
        (
            item.field_id,
            item.unit,
            item.currency,
            item.scope,
        )
        != (first.field_id, first.unit, first.currency, first.scope)
        for item in ordered[1:]
    ):
        raise ValueError("TTM inputs must share field, scope, unit, and currency")
    indices = [_quarter_index(item.period_end) for item in ordered]
    if indices != list(range(indices[0], indices[0] + 4)):
        raise ValueError("TTM inputs must be four consecutive quarters")
    inputs = tuple(
        FormulaInput(item.fact_id, item.value, item.unit, item.period_end)
        for item in ordered
    )
    return NormalizedValue(
        fact_id=fact_id,
        field_id=first.field_id,
        value=sum((item.value for item in ordered), Decimal("0")),
        unit=first.unit,
        currency=first.currency,
        period_start=ordered[0].period_start,
        period_end=ordered[-1].period_end,
        period_kind=PeriodKind.TTM,
        value_kind=ValueKind.FLOW,
        scope=first.scope,
        formula_version=formula_version,
        inputs=inputs,
    )


@dataclass(frozen=True, slots=True)
class RecordVersion:
    dataset_id: str
    row_key: str
    version_key: str
    snapshot_sha256: str
    raw_payload: bytes = field(repr=False)
    retrieved_at: datetime
    available_at: datetime
    source_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.dataset_id or not self.row_key:
            raise ValueError("record dataset and row identities are required")
        object.__setattr__(
            self,
            "snapshot_sha256",
            _require_sha256(self.snapshot_sha256, name="snapshot_sha256"),
        )
        if not self.version_key.startswith("version-") or len(self.version_key) != 72:
            raise ValueError("invalid record version key")
        object.__setattr__(
            self, "retrieved_at", _aware_utc(self.retrieved_at, name="retrieved_at")
        )
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, name="available_at")
        )
        if self.source_updated_at is not None:
            object.__setattr__(
                self,
                "source_updated_at",
                _aware_utc(self.source_updated_at, name="source_updated_at"),
            )
        try:
            json.loads(self.raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("raw_payload must be canonical UTF-8 JSON") from exc

    @classmethod
    def create(
        cls,
        *,
        dataset_id: str,
        raw_row: Mapping[str, Any],
        key_fields: Sequence[str],
        snapshot_sha256: str,
        retrieved_at: datetime,
        available_at: datetime,
        source_updated_at: datetime | None = None,
        partition: Mapping[str, Any] | None = None,
    ) -> "RecordVersion":
        row_key = stable_row_key(
            dataset_id, raw_row, key_fields, partition=partition
        )
        raw_payload = canonical_json_bytes(raw_row)
        version_key = stable_version_key(row_key, raw_row, snapshot_sha256)
        return cls(
            dataset_id=dataset_id,
            row_key=row_key,
            version_key=version_key,
            snapshot_sha256=snapshot_sha256,
            raw_payload=raw_payload,
            retrieved_at=retrieved_at,
            available_at=available_at,
            source_updated_at=source_updated_at,
        )

    @property
    def raw_row(self) -> Mapping[str, Any]:
        return json.loads(self.raw_payload.decode("utf-8"))

    @property
    def revision_at(self) -> datetime:
        return self.source_updated_at or self.retrieved_at


def select_record_version(
    versions: Sequence[RecordVersion],
    *,
    as_of: datetime | None = None,
    strict_point_in_time: bool = False,
) -> RecordVersion | None:
    if not versions:
        return None
    identities = {(item.dataset_id, item.row_key) for item in versions}
    if len(identities) != 1:
        raise ValueError("record-version selection requires one stable row")
    eligible = list(versions)
    if strict_point_in_time:
        if as_of is None:
            raise ValueError("strict point-in-time selection requires as_of")
        cutoff = _aware_utc(as_of, name="as_of")
        eligible = [item for item in eligible if item.available_at <= cutoff]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (item.revision_at, item.retrieved_at, item.version_key),
    )


class RecordCatalogue:
    """Small append-only in-memory model used by storage and protocol tests."""

    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], dict[str, RecordVersion]] = {}

    def append(self, record: RecordVersion) -> bool:
        bucket = self._versions.setdefault(
            (record.dataset_id, record.row_key), {}
        )
        previous = bucket.get(record.version_key)
        if previous is not None:
            if previous != record:
                raise ValueError("an immutable version key cannot be rewritten")
            return False
        bucket[record.version_key] = record
        return True

    def versions(self, dataset_id: str, row_key: str) -> tuple[RecordVersion, ...]:
        bucket = self._versions.get((dataset_id, row_key), {})
        return tuple(sorted(bucket.values(), key=lambda item: item.retrieved_at))

    def select(
        self,
        dataset_id: str,
        row_key: str,
        *,
        as_of: datetime | None = None,
        strict_point_in_time: bool = False,
    ) -> RecordVersion | None:
        return select_record_version(
            self.versions(dataset_id, row_key),
            as_of=as_of,
            strict_point_in_time=strict_point_in_time,
        )
