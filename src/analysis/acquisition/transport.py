from __future__ import annotations

import socket
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from .adapters.base import BoundedTransportEnvelope, QueryWork
from .models import LiveAccessReviewStatus
from .registry import SourceRegistryError, SourceRegistryLoader
from .security import (
    AllowlistEntry,
    ResponseLimits,
    SecurityPolicyError,
    TransportPolicy,
    read_limited_body,
    redact_headers,
    redact_redirect_chain,
    redact_url,
    validate_redirect,
    validate_transport_target,
)
from .source_gate import CrossProcessSourceGate


AddressResolver = Callable[[str, int], Sequence[str]]


def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    }
    return tuple(sorted(addresses))


class RegistryBoundHttpTransport:
    """HTTP transport bound to one immutable SourceDefinition version.

    It never follows redirects implicitly.  Every hop is revalidated and every
    actual request is surrounded by the workspace-wide source gate.  Retries
    are intentionally an orchestrator concern because each protocol retry must
    receive its own durable AcquisitionAttempt.
    """

    def __init__(
        self,
        source_definition: Any,
        source_gate: CrossProcessSourceGate,
        *,
        client: httpx.Client | None = None,
        address_resolver: AddressResolver = _resolve_public_addresses,
        clock=time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source_definition = source_definition
        self.source_gate = source_gate
        self._owns_client = client is None
        timeout = float(source_definition.retry_policy.request_timeout_seconds)
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
        )
        self._address_resolver = address_resolver
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._initial_policy = TransportPolicy(
            allowlist=tuple(
                _allowlist_entry(item)
                for item in source_definition.initial_request_allowlist
            ),
            max_redirects=0,
        )
        self._redirect_policy = TransportPolicy(
            allowlist=tuple(
                _allowlist_entry(item)
                for item in source_definition.redirect_allowlist
            ),
            max_redirects=int(source_definition.response_limits.max_redirects),
        )
        self._limits = ResponseLimits(
            max_compressed_bytes=int(
                source_definition.response_limits.max_compressed_bytes
            ),
            max_decompressed_bytes=int(
                source_definition.response_limits.max_decompressed_bytes
            ),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "RegistryBoundHttpTransport":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(self, work: QueryWork) -> BoundedTransportEnvelope:
        review = getattr(self.source_definition, "live_access_review", None)
        review_status = getattr(review, "status", None)
        if getattr(review_status, "value", review_status) != LiveAccessReviewStatus.APPROVED.value:
            # This check deliberately precedes DNS resolution, the shared
            # source gate and client.build_request/send.  Registry prose or an
            # allowlist alone can never authorize a live request.
            raise SecurityPolicyError(
                "manual_access_review_required",
                "来源尚未完成版本化live access人工审核，禁止联网",
            )
        if work.source_definition_id != self.source_definition.source_definition_id:
            raise ValueError("query work source does not match bound transport")
        if str(work.source_definition_version) != str(self.source_definition.version):
            raise ValueError("query work source version does not match bound transport")
        definition_limit = int(self.source_definition.response_limits.max_response_bytes)
        if work.max_response_bytes > definition_limit:
            raise ValueError("query work may not raise the frozen response limit")
        if work.method not in {"GET", "HEAD", "POST"}:
            raise ValueError("unsupported request method")

        capability = work.execution_capability
        if capability is None:
            # QueryWork is also used by pure parser tests, so construction may
            # omit an execution capability.  The real HTTP primitive must not:
            # absence is rejected before DNS, source-gate acquisition or send.
            raise SecurityPolicyError(
                "execution_capability_required",
                "真实来源请求缺少由执行器签发的run/attempt/lease能力凭证",
            )
        if (
            capability.source_definition_id != work.source_definition_id
            or str(capability.source_definition_version)
            != str(work.source_definition_version)
            or capability.source_definition_id
            != self.source_definition.source_definition_id
            or str(capability.source_definition_version)
            != str(self.source_definition.version)
        ):
            raise SecurityPolicyError(
                "execution_capability_mismatch",
                "执行能力凭证与固定来源定义不一致",
            )
        try:
            SourceRegistryLoader.assert_effective(
                self.source_definition,
                capability.run_as_of,
            )
        except (AttributeError, SourceRegistryError, TypeError, ValueError) as exc:
            raise SecurityPolicyError(
                "source_definition_not_effective",
                "固定来源定义对执行能力凭证中的run as_of无效",
            ) from exc

        lease_guard = capability.guard

        def guard(*, force: bool = False) -> None:
            lease_guard(force=force)

        deadline = float(
            work.context.get(
                "deadline_monotonic",
                self._clock()
                + float(self.source_definition.retry_policy.attempt_deadline_seconds),
            )
        )

        def remaining_seconds() -> float:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise httpx.TimeoutException("attempt deadline reached during transport")
            return remaining

        def response_progress() -> None:
            guard()
            remaining_seconds()

        current_url = work.url
        redirect_urls: list[str] = []
        guard(force=True)
        resolved = self._resolve(current_url)
        guard(force=True)
        validated = validate_transport_target(
            current_url,
            self._initial_policy,
            resolved_addresses=resolved,
        )
        redirect_count = 0
        while True:
            remaining_seconds()
            with self.source_gate.hold(
                work.source_definition_id,
                validated.host,
                min_interval_seconds=float(
                    self.source_definition.rate_limit.min_interval_seconds
                ),
                deadline_monotonic=deadline,
                lease_guard=lease_guard,
                upstream_identity=getattr(
                    self.source_definition, "upstream_identity", None
                ),
            ):
                # The gate may have blocked longer than the lease TTL.  This
                # forced repository fence is the last operation before send.
                guard(force=True)
                request = self._client.build_request(
                    work.method,
                    validated.url,
                    params=work.params or None,
                    json=work.json_body,
                    data=work.form_body,
                    headers=dict(work.headers),
                    timeout=min(
                        float(self.source_definition.retry_policy.request_timeout_seconds),
                        remaining_seconds(),
                    ),
                )
                response = self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
                try:
                    response_progress()
                    # HTTPX classifies every 3xx as is_redirect. 304 is a
                    # validator response and has no Location or message body.
                    not_modified = response.status_code == 304
                    if response.is_redirect and not not_modified:
                        location = response.headers.get("location")
                        if not location:
                            raise SecurityPolicyError(
                                "redirect_not_allowlisted",
                                "重定向响应缺少 Location",
                            )
                        guard(force=True)
                        redirect_addresses = self._resolve_redirect(
                            str(response.request.url), location
                        )
                        guard(force=True)
                        next_target = validate_redirect(
                            str(response.request.url),
                            location,
                            self._redirect_policy,
                            redirect_count=redirect_count,
                            resolved_addresses=redirect_addresses,
                        )
                        redirect_urls.append(next_target.url)
                        redirect_count += 1
                        validated = next_target
                        # Redirected GET/HEAD keeps no request body.  307/308 keep
                        # the method, while 301/302/303 are normalized to GET.
                        if response.status_code in {301, 302, 303} and work.method != "HEAD":
                            work = QueryWork(
                                source_definition_id=work.source_definition_id,
                                source_definition_version=work.source_definition_version,
                                query_id=work.query_id,
                                query_family=work.query_family,
                                execution_key=work.execution_key,
                                method="GET",
                                url=next_target.url,
                                page=work.page,
                                cursor=work.cursor,
                                headers=work.headers,
                                expected_mime_types=work.expected_mime_types,
                                max_response_bytes=work.max_response_bytes,
                                parser_schema_version=work.parser_schema_version,
                                context=work.context,
                                execution_capability=work.execution_capability,
                            )
                        continue
                    chunks = () if not_modified else (
                        (response.content,) if response.is_stream_consumed
                        else response.iter_raw()
                    )

                    def guarded_chunks():
                        iterator = iter(chunks)
                        while True:
                            response_progress()
                            try:
                                chunk = next(iterator)
                            except StopIteration:
                                response_progress()
                                return
                            response_progress()
                            yield chunk

                    body = read_limited_body(
                        guarded_chunks(),
                        self._limits,
                        # In a 304 these describe the selected representation,
                        # not transferred bytes; there is no body to decode.
                        content_encoding=None if not_modified else response.headers.get("content-encoding"),
                        content_length=None if not_modified else response.headers.get("content-length"),
                    )
                    # Decompression/final assembly also consumes the budget.
                    # Blocking reads keep their socket timeout; once control
                    # returns, an expired attempt can never publish an envelope.
                    response_progress()
                    if len(body) > work.max_response_bytes:
                        from .security import ResponseSizeExceeded

                        raise ResponseSizeExceeded(
                            "响应超过query声明的max_response_bytes"
                        )
                    now = self._wall_clock()
                    return BoundedTransportEnvelope(
                        request_url=redact_url(str(response.request.url)),
                        final_url=redact_url(str(response.url)),
                        status_code=response.status_code,
                        headers=redact_headers(response.headers),
                        body=body,
                        redirect_chain=redact_redirect_chain(redirect_urls),
                        observed_at=now,
                        retrieved_at=now,
                        body_limit=work.max_response_bytes,
                    )
                finally:
                    response.close()

    def _resolve(self, url: str) -> Sequence[str]:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return ()
        return self._address_resolver(parsed.hostname, parsed.port or 443)

    def _resolve_redirect(self, current_url: str, location: str) -> Sequence[str]:
        from urllib.parse import urljoin

        return self._resolve(urljoin(current_url, location))


def _allowlist_entry(value: Any) -> AllowlistEntry:
    path_prefix = getattr(value, "path_prefix", "/")
    return AllowlistEntry(
        host=str(value.host),
        path_prefixes=(str(path_prefix),),
        scheme=str(getattr(value, "scheme", "https")),
        ports=(int(getattr(value, "port", 443)),),
        allow_subdomains=False,
    )
