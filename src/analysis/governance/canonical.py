from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from pydantic import BaseModel, TypeAdapter


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented by the canonical JSON contract."""


def ensure_aware_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime and reject timestamps without provenance."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalizationError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def canonical_datetime(value: datetime) -> str:
    """Serialize an aware timestamp as RFC3339 UTC using a stable ``Z`` suffix."""

    normalized = ensure_aware_utc(value)
    rendered = normalized.isoformat(timespec="microseconds")
    if normalized.microsecond == 0:
        rendered = normalized.isoformat(timespec="seconds")
    return rendered.removesuffix("+00:00") + "Z"


def shanghai_date_exclusive_upper_bound(value: date) -> datetime:
    """Conservatively normalize a Shanghai date to the next local-day boundary."""

    if isinstance(value, datetime):
        raise CanonicalizationError("date precision requires a date, not datetime")
    next_midnight = datetime.combine(
        value + timedelta(days=1), time.min, tzinfo=SHANGHAI_TIMEZONE
    )
    return next_midnight.astimezone(timezone.utc)


def canonical_decimal(value: Decimal) -> str:
    """Return a finite, non-exponent decimal string without binary-float drift."""

    if not isinstance(value, Decimal):
        raise CanonicalizationError("decimal values must be Decimal instances")
    if not value.is_finite():
        raise CanonicalizationError("decimal values must be finite")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"-0", ""}:
        return "0"
    return rendered


def _model_payload(value: BaseModel) -> dict[str, Any]:
    computed = set(type(value).model_computed_fields)
    return value.model_dump(
        mode="python",
        by_alias=True,
        exclude=computed,
    )


def canonicalize(value: Any) -> Any:
    """Convert supported values into the repository's canonical JSON value space."""

    if isinstance(value, BaseModel):
        return canonicalize(_model_payload(value))
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, datetime):
        return canonical_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if is_dataclass(value) and not isinstance(value, type):
        return canonicalize(asdict(value))
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("canonical JSON object keys must be strings")
            normalized[key] = canonicalize(item)
        return normalized
    if isinstance(value, (set, frozenset)):
        items = [canonicalize(item) for item in value]
        return sorted(items, key=canonical_json_bytes)
    if isinstance(value, (tuple, list)):
        return [canonicalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("floating-point values must be finite")
        raise CanonicalizationError(
            "floating-point values are not canonical; use Decimal for non-integers"
        )
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise CanonicalizationError(
        f"unsupported canonical JSON value: {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a value as stable UTF-8 JSON with sorted object keys."""

    try:
        rendered = json.dumps(
            canonicalize(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CanonicalizationError):
            raise
        raise CanonicalizationError(str(exc)) from exc
    return rendered.encode("utf-8")


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def canonical_sha256(
    value: Any,
    *,
    schema_name: str,
    schema_version: str,
) -> str:
    """Hash a payload together with its explicit schema identity."""

    if not isinstance(schema_name, str) or not schema_name.strip():
        raise CanonicalizationError("schema_name must be a non-empty string")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise CanonicalizationError("schema_version must be a non-empty string")
    envelope = {
        "payload": value,
        "schema_name": schema_name,
        "schema_version": schema_version,
    }
    return hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()


def content_addressed_id(
    prefix: str,
    value: Any,
    *,
    schema_name: str,
    schema_version: str,
) -> str:
    if not prefix or ":" in prefix or any(char.isspace() for char in prefix):
        raise CanonicalizationError("content-addressed ID prefix is invalid")
    return f"{prefix}:{canonical_sha256(value, schema_name=schema_name, schema_version=schema_version)}"


def load_canonical_json(data: bytes | bytearray | memoryview | str) -> Any:
    """Parse JSON only when the original bytes already use canonical encoding."""

    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
    else:
        raise CanonicalizationError("canonical JSON input must be UTF-8 bytes or text")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalizationError(f"invalid UTF-8 JSON: {exc}") from exc
    if canonical_json_bytes(value) != raw:
        raise CanonicalizationError("JSON payload is valid but not canonical")
    return value


def validate_canonical_json(
    adapter: TypeAdapter[Any],
    data: bytes | bytearray | memoryview | str,
) -> Any:
    """Fail closed on non-canonical bytes before Pydantic union validation."""

    load_canonical_json(data)
    return adapter.validate_json(data, strict=True)


__all__ = [
    "CanonicalizationError",
    "SHANGHAI_TIMEZONE",
    "canonical_datetime",
    "canonical_decimal",
    "canonical_json_bytes",
    "canonical_json_text",
    "canonical_sha256",
    "canonicalize",
    "content_addressed_id",
    "ensure_aware_utc",
    "load_canonical_json",
    "shanghai_date_exclusive_upper_bound",
    "validate_canonical_json",
]
