from __future__ import annotations

import hashlib
import ipaddress
import posixpath
import re
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
    }
)
_SENSITIVE_NAME_PATTERN = re.compile(
    r"(?:^|[-_])(?:access[-_]?token|api[-_]?key|auth|authorization|cookie|credential|"
    r"csrf|jwt|password|secret|session|signature|token)(?:$|[-_])",
    re.IGNORECASE,
)


class SecurityPolicyError(ValueError):
    """A deterministic, non-retryable local transport-policy rejection."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ResponseSizeExceeded(SecurityPolicyError):
    def __init__(self, message: str) -> None:
        super().__init__("response_size_exceeded", message)


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """One exact transport boundary from a frozen SourceDefinition version."""

    host: str
    path_prefixes: tuple[str, ...] = ("/",)
    scheme: str = "https"
    ports: tuple[int, ...] = (443,)
    allow_subdomains: bool = False

    def __post_init__(self) -> None:
        host = _normalize_host(self.host)
        if not host or "/" in host:
            raise ValueError("allowlist host 必须是单个规范域名")
        if self.scheme.lower() != "https":
            raise ValueError("采集 allowlist 只允许 HTTPS")
        if not self.ports or any(port < 1 or port > 65535 for port in self.ports):
            raise ValueError("allowlist port 非法")
        if not self.path_prefixes:
            raise ValueError("allowlist 至少需要一个路径前缀")
        prefixes = tuple(_normalize_allowed_prefix(item) for item in self.path_prefixes)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "scheme", "https")
        object.__setattr__(self, "ports", tuple(sorted(set(self.ports))))
        object.__setattr__(self, "path_prefixes", prefixes)


@dataclass(frozen=True, slots=True)
class TransportPolicy:
    allowlist: tuple[AllowlistEntry, ...]
    max_redirects: int = 5
    approved_private_addresses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.allowlist:
            raise ValueError("transport policy 必须包含 allowlist")
        if self.max_redirects < 0:
            raise ValueError("max_redirects 不能为负数")
        normalized: list[str] = []
        for value in self.approved_private_addresses:
            normalized.append(str(ipaddress.ip_address(value)))
        object.__setattr__(
            self,
            "approved_private_addresses",
            tuple(sorted(set(normalized))),
        )


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    url: str
    scheme: str
    host: str
    port: int
    path: str


@dataclass(frozen=True, slots=True)
class ResponseLimits:
    max_compressed_bytes: int
    max_decompressed_bytes: int

    def __post_init__(self) -> None:
        if self.max_compressed_bytes <= 0 or self.max_decompressed_bytes <= 0:
            raise ValueError("响应大小上限必须为正数")


@dataclass(frozen=True, slots=True)
class MinimalResponseDiagnostic:
    status_code: int
    content_type: str | None
    body_sha256: str
    body_length: int
    classification_hint: str | None
    headers: dict[str, str]


def is_sensitive_name(name: str) -> bool:
    normalized = name.strip().lower()
    return normalized in _SENSITIVE_HEADER_NAMES or bool(
        _SENSITIVE_NAME_PATTERN.search(normalized)
    )


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Return a stable, safe HTTP summary without authentication material."""

    if not headers:
        return {}
    redacted: dict[str, str] = {}
    for key, value in sorted(headers.items(), key=lambda item: str(item[0]).lower()):
        name = str(key).strip()
        if not name:
            continue
        redacted[name] = REDACTED if is_sensitive_name(name) else str(value)
    return redacted


def redact_url(url: str) -> str:
    """Remove URL credentials and redact sensitive query values before persistence."""

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    query = urlencode(
        [
            (name, REDACTED if is_sensitive_name(name) else value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
        quote_via=quote,
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def redact_redirect_chain(urls: Sequence[str]) -> tuple[str, ...]:
    return tuple(redact_url(url) for url in urls)


def validate_transport_target(
    url: str,
    policy: TransportPolicy,
    *,
    resolved_addresses: Sequence[str] | None = None,
) -> ValidatedTarget:
    """Validate a request target immediately before performing that network hop."""

    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise SecurityPolicyError(
            "transport_target_forbidden", "采集目标禁止 HTTPS 降级"
        )
    if parsed.username is not None or parsed.password is not None:
        raise SecurityPolicyError(
            "transport_target_forbidden", "采集 URL 不得携带凭据"
        )
    if not parsed.hostname:
        raise SecurityPolicyError(
            "transport_target_forbidden", "采集 URL 缺少主机名"
        )
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise SecurityPolicyError(
            "transport_target_forbidden", "采集 URL 端口非法"
        ) from exc
    host = _normalize_host(parsed.hostname)
    path = _normalize_request_path(parsed.path)
    matching = [
        entry
        for entry in policy.allowlist
        if _host_matches(host, entry) and port in entry.ports
    ]
    if not matching or not any(
        _path_matches(path, prefix) for entry in matching for prefix in entry.path_prefixes
    ):
        raise SecurityPolicyError(
            "redirect_not_allowlisted",
            f"目标不在固定来源版本 allowlist: {host}{path}",
        )

    addresses = list(resolved_addresses or ())
    try:
        addresses.append(str(ipaddress.ip_address(host)))
    except ValueError:
        pass
    approved_private = set(policy.approved_private_addresses)
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise SecurityPolicyError(
                "transport_target_forbidden", "目标 DNS 结果不是合法 IP 地址"
            ) from exc
        if _is_forbidden_address(address) and str(address) not in approved_private:
            raise SecurityPolicyError(
                "transport_target_forbidden",
                "目标解析到未批准的 private/loopback/link-local 地址",
            )
    return ValidatedTarget(
        url=urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, "")),
        scheme="https",
        host=host,
        port=port,
        path=path,
    )


def validate_redirect(
    current_url: str,
    location: str,
    policy: TransportPolicy,
    *,
    redirect_count: int,
    resolved_addresses: Sequence[str] | None = None,
) -> ValidatedTarget:
    """Resolve and validate Location before the caller sends the next request."""

    if redirect_count >= policy.max_redirects:
        raise SecurityPolicyError(
            "redirect_not_allowlisted", "重定向跳数超过来源定义上限"
        )
    target = urljoin(current_url, location)
    return validate_transport_target(
        target,
        policy,
        resolved_addresses=resolved_addresses,
    )


def validate_content_length(
    content_length: str | int | None,
    limits: ResponseLimits,
) -> int | None:
    if content_length in {None, ""}:
        return None
    try:
        length = int(content_length)
    except (TypeError, ValueError) as exc:
        raise SecurityPolicyError(
            "invalid_content_length", "Content-Length 不是非负整数"
        ) from exc
    if length < 0:
        raise SecurityPolicyError(
            "invalid_content_length", "Content-Length 不是非负整数"
        )
    if length > limits.max_compressed_bytes:
        raise ResponseSizeExceeded("Content-Length 超过来源定义硬上限")
    return length


def read_limited_body(
    chunks: Iterable[bytes],
    limits: ResponseLimits,
    *,
    content_encoding: str | None = None,
    content_length: str | int | None = None,
) -> bytes:
    """Read a response once while enforcing compressed and decompressed limits."""

    declared_length = validate_content_length(content_length, limits)
    encoding = (content_encoding or "identity").strip().lower()
    decompressor: zlib.decompressobj | None
    if encoding in {"", "identity"}:
        decompressor = None
    elif encoding in {"gzip", "x-gzip"}:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()
    else:
        raise SecurityPolicyError(
            "unsupported_content_encoding", f"不支持的 Content-Encoding: {encoding}"
        )

    compressed_count = 0
    output = bytearray()
    try:
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("响应 chunk 必须为 bytes")
            compressed_count += len(chunk)
            if compressed_count > limits.max_compressed_bytes:
                raise ResponseSizeExceeded("流式压缩响应超过来源定义硬上限")
            if decompressor is None:
                output.extend(chunk)
                if len(output) > limits.max_decompressed_bytes:
                    raise ResponseSizeExceeded("流式解压响应超过来源定义硬上限")
                continue
            pending = chunk
            while pending:
                remaining = limits.max_decompressed_bytes - len(output)
                decoded = decompressor.decompress(pending, remaining + 1)
                output.extend(decoded)
                if len(output) > limits.max_decompressed_bytes:
                    raise ResponseSizeExceeded("流式解压响应超过来源定义硬上限")
                next_pending = decompressor.unconsumed_tail
                if not next_pending:
                    break
                if next_pending == pending and not decoded:
                    raise SecurityPolicyError(
                        "invalid_compressed_response", "响应解压没有取得进展"
                    )
                pending = next_pending
        if decompressor is not None:
            remaining = limits.max_decompressed_bytes - len(output)
            output.extend(decompressor.flush(remaining + 1))
            if len(output) > limits.max_decompressed_bytes:
                raise ResponseSizeExceeded("流式解压响应超过来源定义硬上限")
            if not decompressor.eof:
                raise SecurityPolicyError(
                    "truncated_response", "压缩响应未完整结束"
                )
            if decompressor.unused_data:
                raise SecurityPolicyError(
                    "invalid_compressed_response", "压缩响应包含未校验的尾随数据"
                )
    except zlib.error as exc:
        raise SecurityPolicyError("invalid_compressed_response", "响应解压失败") from exc
    if declared_length is not None and compressed_count != declared_length:
        raise SecurityPolicyError(
            "content_length_mismatch", "实际响应长度与 Content-Length 不一致"
        )
    return bytes(output)


def minimal_response_diagnostic(
    *,
    status_code: int,
    headers: Mapping[str, Any] | None,
    body: bytes,
    expected_mime_types: Sequence[str] = (),
) -> MinimalResponseDiagnostic:
    """Build the allowed audit summary for a rejected/limited response."""

    safe_headers = redact_headers(headers)
    content_type = _base_content_type(safe_headers)
    prefix = body[:4096].lower()
    hint: str | None = None
    if status_code == 401 or b"type=\"password\"" in prefix or b"name=\"password\"" in prefix:
        hint = "login_required"
    elif status_code == 402 or any(marker in prefix for marker in (b"paywall", b"subscribe to", b"payment required")):
        hint = "paywalled"
    elif status_code == 429:
        hint = "rate_limited"
    elif status_code == 403 or any(
        marker in prefix
        for marker in (b"captcha", b"recaptcha", b"verify you are human", b"javascript challenge")
    ):
        hint = "restricted"
    elif expected_mime_types and content_type not in {
        item.lower().split(";", 1)[0].strip() for item in expected_mime_types
    }:
        hint = "unexpected_mime"
    return MinimalResponseDiagnostic(
        status_code=status_code,
        content_type=content_type,
        body_sha256=hashlib.sha256(body).hexdigest(),
        body_length=len(body),
        classification_hint=hint,
        headers=safe_headers,
    )


def sanitize_http_metadata(value: Any) -> Any:
    """Recursively redact mappings before they enter an observation or API payload."""

    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if is_sensitive_name(str(key))
                else sanitize_http_metadata(item)
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_http_metadata(item) for item in value]
    return value


def _normalize_host(host: str) -> str:
    value = host.strip().rstrip(".").lower()
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("域名无法规范化") from exc


def _normalize_allowed_prefix(prefix: str) -> str:
    if not prefix.startswith("/"):
        raise ValueError("allowlist path prefix 必须以 / 开头")
    return _normalize_request_path(prefix)


def _normalize_request_path(path: str) -> str:
    decoded = unquote(path or "/").replace("\\", "/")
    if any(segment == ".." for segment in decoded.split("/")):
        raise SecurityPolicyError(
            "transport_target_forbidden", "URL 路径包含穿越片段"
        )
    normalized = posixpath.normpath(decoded)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if decoded.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def _host_matches(host: str, entry: AllowlistEntry) -> bool:
    return host == entry.host or (
        entry.allow_subdomains and host.endswith("." + entry.host)
    )


def _path_matches(path: str, prefix: str) -> bool:
    if prefix == "/":
        return True
    base = prefix.rstrip("/")
    return path == base or path.startswith(base + "/")


def _is_forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _base_content_type(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.lower().split(";", 1)[0].strip()
    return None
