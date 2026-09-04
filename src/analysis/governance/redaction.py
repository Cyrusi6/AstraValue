from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel


REDACTED = "[REDACTED]"
REDACTED_PATH = "[REDACTED_PATH]"
REDACTED_QUERY_VALUE = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "browser_config",
        "browser_configuration",
        "browser_profile",
        "chain_of_thought",
        "cookie",
        "cookies",
        "credentials",
        "hidden_reasoning",
        "owner_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "set_cookie",
        "token",
    }
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)[^\r\n<>\"'|?*,;]+"
)
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:data|etc|home|mnt|opt|private|root|srv|tmp|Users|var|workspace)"
    r"(?:/[^\r\n<>\"'|,;]*)?"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|owner[-_ ]?token|access[-_ ]?token|"
    r"refresh[-_ ]?token|api[-_ ]?key|client[-_ ]?secret|private[-_ ]?key|"
    r"password|secret)\s*([:=])\s*"
    r"([^\r\n]+)"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _normalized_key(value: object) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return key


def is_sensitive_key(value: object) -> bool:
    key = _normalized_key(value)
    return key in _SENSITIVE_KEYS or key.endswith(
        ("_api_key", "_password", "_private_key", "_secret", "_token")
    )


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,)]:":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return REDACTED + trailing

    hostname = parsed.hostname or ""
    if not hostname:
        return REDACTED + trailing
    host = hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    query = urlencode(
        [(key, REDACTED_QUERY_VALUE) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)],
        doseq=True,
    )
    safe = urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    return safe + trailing


def redact_text(value: str) -> str:
    """Return deterministic display-safe text without credentials or private paths."""

    if not isinstance(value, str):
        raise TypeError("redact_text expects str")
    text = value.replace("\ufffd", "[INVALID_TEXT]")
    text = _CONTROL_RE.sub("", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _OPENAI_TOKEN_RE.sub(REDACTED, text)
    text = _INLINE_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    text = _URL_RE.sub(_redact_url, text)
    text = _WINDOWS_PATH_RE.sub(REDACTED_PATH, text)
    text = _POSIX_PRIVATE_PATH_RE.sub(REDACTED_PATH, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact a structured value while preserving deterministic shape."""

    if isinstance(value, BaseModel):
        return redact_value(value.model_dump(mode="python"))
    if is_dataclass(value) and not isinstance(value, type):
        return redact_value(asdict(value))
    if isinstance(value, Enum):
        return redact_value(value.value)
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if not isinstance(key, str):
                raise TypeError("safe redaction requires string mapping keys")
            rendered_key = redact_text(key)
            if rendered_key in redacted:
                raise ValueError("redaction produced duplicate mapping keys")
            redacted[rendered_key] = REDACTED if is_sensitive_key(key) else redact_value(item)
        return redacted
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((redact_value(item) for item in value), key=str))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[BINARY_REDACTED:{len(value)} bytes]"
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, date, datetime, Decimal)):
        return value
    raise TypeError(f"unsupported value for safe redaction: {type(value).__name__}")


__all__ = [
    "REDACTED",
    "REDACTED_PATH",
    "is_sensitive_key",
    "redact_text",
    "redact_value",
]
