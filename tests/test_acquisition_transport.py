from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from analysis.acquisition.adapters import QueryWork, TransportExecutionCapability
from analysis.acquisition.repository import StaleLeaseError
from analysis.acquisition.security import SecurityPolicyError, ResponseSizeExceeded
from analysis.acquisition.source_gate import CrossProcessSourceGate
from analysis.acquisition.transport import RegistryBoundHttpTransport


def _definition(
    *,
    max_bytes=4096,
    min_interval=0.001,
    effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    expires_at=None,
):
    rule = SimpleNamespace(
        host="approved.example",
        path_prefix="/api",
        scheme="https",
        port=443,
    )
    redirect_rule = SimpleNamespace(
        host="files.example",
        path_prefix="/public",
        scheme="https",
        port=443,
    )
    return SimpleNamespace(
        source_definition_id="test.source",
        version="1",
        effective_at=effective_at,
        expires_at=expires_at,
        live_access_review=SimpleNamespace(status="approved"),
        initial_request_allowlist=(rule,),
        redirect_allowlist=(rule, redirect_rule),
        response_limits=SimpleNamespace(
            max_response_bytes=max_bytes,
            max_compressed_bytes=max_bytes,
            max_decompressed_bytes=max_bytes,
            max_redirects=3,
        ),
        rate_limit=SimpleNamespace(min_interval_seconds=min_interval),
        retry_policy=SimpleNamespace(
            request_timeout_seconds=2,
            attempt_deadline_seconds=3,
        ),
    )


@pytest.mark.parametrize(
    "source_id",
    ("cninfo.disclosures", "sse.disclosures", "szse.disclosures"),
)
def test_pending_manual_review_blocks_v1_source_before_dns_gate_and_send(
    tmp_path, source_id
) -> None:
    from analysis.acquisition.registry import SourceRegistryLoader

    definition = SourceRegistryLoader().load_registry().definition(source_id)
    query = definition.queries[0]
    rejected_urls = {
        "cninfo.disclosures": "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        "sse.disclosures": "https://query.sse.com.cn/security/stock/queryCompanyStatementNew.do",
        "szse.disclosures": "https://www.szse.cn/api/disc/announcement/annList",
    }
    rejected_rules = {
        "cninfo.disclosures": ("www.cninfo.com.cn", "/new/"),
        "sse.disclosures": ("query.sse.com.cn", "/security/stock/"),
        "szse.disclosures": ("www.szse.cn", "/api/disc/"),
    }
    host, path_prefix = rejected_rules[source_id]
    rejected_rule = SimpleNamespace(
        scheme="https",
        host=host,
        port=443,
        path_prefix=path_prefix,
    )
    # The reviewed registry deliberately carries no network authority.  Add a
    # test-only route so this unit test isolates the review gate and proves it
    # still runs before DNS, the source gate, or send.
    definition = definition.model_copy(
        update={
            "initial_request_allowlist": (rejected_rule,),
            "redirect_allowlist": (rejected_rule,),
        }
    )
    dns_calls = []
    send_calls = []

    def handler(request):
        send_calls.append(str(request.url))
        return httpx.Response(200, content=b"{}", request=request)

    transport = RegistryBoundHttpTransport(
        definition,
        CrossProcessSourceGate(
            tmp_path / "workspace",
            lock_root=tmp_path / "locks",
            poll_interval_seconds=0.001,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        address_resolver=lambda host, port: dns_calls.append((host, port)) or ("8.8.8.8",),
    )
    work = QueryWork(
        source_definition_id=definition.source_definition_id,
        source_definition_version=definition.version,
        query_id=query.query_id,
        query_family=query.query_family,
        execution_key=query.execution_key,
        method=query.request_method,
        url=rejected_urls[source_id],
        max_response_bytes=definition.response_limits.max_response_bytes,
    )

    with pytest.raises(SecurityPolicyError) as caught:
        transport.request(work)

    assert caught.value.reason_code == "manual_access_review_required"
    assert dns_calls == []
    assert send_calls == []


def _capability(**changes):
    values = dict(
        run_id="run-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        run_as_of=datetime(2026, 9, 4, tzinfo=timezone.utc),
        source_definition_id="test.source",
        source_definition_version="1",
        lease_guard=lambda *, force=False: None,
    )
    values.update(changes)
    return TransportExecutionCapability(**values)


def _work(**changes):
    values = dict(
        source_definition_id="test.source",
        source_definition_version="1",
        query_id="q",
        query_family="qf",
        execution_key="ek",
        method="GET",
        url="https://approved.example/api/list",
        max_response_bytes=4096,
        execution_capability=_capability(),
    )
    values.update(changes)
    return QueryWork(**values)


def _transport(
    tmp_path,
    handler,
    *,
    address_resolver=None,
    **definition_changes,
):
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    return RegistryBoundHttpTransport(
        _definition(**definition_changes),
        CrossProcessSourceGate(
            tmp_path / "workspace",
            lock_root=tmp_path / "locks",
            poll_interval_seconds=0.001,
        ),
        client=client,
        address_resolver=(
            address_resolver
            if address_resolver is not None
            else lambda host, port: ("93.184.216.34",)
        ),
    )


def test_missing_execution_capability_blocks_before_dns_or_send(tmp_path) -> None:
    dns_calls = []
    send_calls = []

    def handler(request):
        send_calls.append(str(request.url))
        return httpx.Response(200, content=b"{}", request=request)

    transport = _transport(
        tmp_path,
        handler,
        address_resolver=lambda host, port: dns_calls.append((host, port))
        or ("93.184.216.34",),
    )
    with pytest.raises(SecurityPolicyError) as caught:
        transport.request(_work(execution_capability=None))

    assert caught.value.reason_code == "execution_capability_required"
    assert dns_calls == []
    assert send_calls == []


def test_invalid_execution_lease_blocks_before_dns_or_send(tmp_path) -> None:
    dns_calls = []
    send_calls = []

    def stale_guard(*, force=False):
        raise StaleLeaseError("stale fixture lease")

    def handler(request):
        send_calls.append(str(request.url))
        return httpx.Response(200, content=b"{}", request=request)

    transport = _transport(
        tmp_path,
        handler,
        address_resolver=lambda host, port: dns_calls.append((host, port))
        or ("93.184.216.34",),
    )
    with pytest.raises(StaleLeaseError):
        transport.request(
            _work(execution_capability=_capability(lease_guard=stale_guard))
        )

    assert dns_calls == []
    assert send_calls == []


@pytest.mark.parametrize(
    ("effective_at", "expires_at"),
    (
        (datetime(2026, 9, 5, tzinfo=timezone.utc), None),
        (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 4, tzinfo=timezone.utc),
        ),
    ),
)
def test_definition_must_be_effective_for_capability_run_as_of_before_dns_or_send(
    tmp_path,
    effective_at,
    expires_at,
) -> None:
    dns_calls = []
    send_calls = []

    def handler(request):
        send_calls.append(str(request.url))
        return httpx.Response(200, content=b"{}", request=request)

    transport = _transport(
        tmp_path,
        handler,
        effective_at=effective_at,
        expires_at=expires_at,
        address_resolver=lambda host, port: dns_calls.append((host, port))
        or ("93.184.216.34",),
    )
    with pytest.raises(SecurityPolicyError) as caught:
        transport.request(_work())

    assert caught.value.reason_code == "source_definition_not_effective"
    assert dns_calls == []
    assert send_calls == []


@pytest.mark.parametrize(
    "capability_change",
    (
        {"source_definition_id": "other.source"},
        {"source_definition_version": "2"},
    ),
)
def test_capability_source_identity_mismatch_blocks_before_dns_or_send(
    tmp_path,
    capability_change,
) -> None:
    dns_calls = []
    send_calls = []

    def handler(request):
        send_calls.append(str(request.url))
        return httpx.Response(200, content=b"{}", request=request)

    transport = _transport(
        tmp_path,
        handler,
        address_resolver=lambda host, port: dns_calls.append((host, port))
        or ("93.184.216.34",),
    )
    with pytest.raises(SecurityPolicyError) as caught:
        transport.request(
            _work(execution_capability=_capability(**capability_change))
        )

    assert caught.value.reason_code == "execution_capability_mismatch"
    assert dns_calls == []
    assert send_calls == []


def test_redirect_is_checked_before_second_hop(tmp_path) -> None:
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "approved.example":
            return httpx.Response(
                302,
                headers={"location": "https://files.example/public/report.pdf"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"%PDF-safe",
            headers={"content-type": "application/pdf"},
            request=request,
        )

    envelope = _transport(tmp_path, handler).request(_work())
    assert len(seen) == 2
    assert envelope.final_url == "https://files.example/public/report.pdf"
    assert envelope.redirect_chain == ("https://files.example/public/report.pdf",)


def test_unallowlisted_redirect_never_requests_target(tmp_path) -> None:
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://evil.example/private"},
            request=request,
        )

    with pytest.raises(SecurityPolicyError) as caught:
        _transport(tmp_path, handler).request(_work())
    assert caught.value.reason_code == "redirect_not_allowlisted"
    assert len(seen) == 1


def test_private_target_is_rejected_before_io(tmp_path) -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"{}", request=request)

    transport = RegistryBoundHttpTransport(
        _definition(),
        CrossProcessSourceGate(tmp_path / "workspace", lock_root=tmp_path / "locks"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        address_resolver=lambda host, port: ("127.0.0.1",),
    )
    with pytest.raises(SecurityPolicyError) as caught:
        transport.request(_work())
    assert caught.value.reason_code == "transport_target_forbidden"
    assert calls == 0


def test_streaming_response_limit_publishes_no_envelope(tmp_path) -> None:
    def handler(request):
        return httpx.Response(200, content=b"x" * 33, request=request)

    with pytest.raises(ResponseSizeExceeded):
        _transport(tmp_path, handler, max_bytes=32).request(
            _work(max_response_bytes=32)
        )


def test_sensitive_query_and_headers_are_redacted_in_envelope(tmp_path) -> None:
    def handler(request):
        return httpx.Response(
            200,
            content=b"{}",
            headers={"content-type": "application/json", "set-cookie": "secret"},
            request=request,
        )

    envelope = _transport(tmp_path, handler).request(
        _work(params={"token": "secret", "page": "1"})
    )
    assert "secret" not in envelope.request_url
    assert envelope.headers["set-cookie"] == "[REDACTED]"
