from __future__ import annotations

import gzip

import pytest

from analysis.acquisition.security import (
    REDACTED,
    AllowlistEntry,
    ResponseLimits,
    ResponseSizeExceeded,
    SecurityPolicyError,
    TransportPolicy,
    minimal_response_diagnostic,
    read_limited_body,
    redact_headers,
    redact_redirect_chain,
    redact_url,
    validate_content_length,
    validate_redirect,
    validate_transport_target,
)
from analysis.acquisition.snapshots import ContentAddressedBlobStore


def _policy() -> TransportPolicy:
    return TransportPolicy(
        allowlist=(
            AllowlistEntry(
                host="example.test",
                path_prefixes=("/disclosures",),
            ),
        ),
        max_redirects=2,
    )


def test_redaction_removes_headers_query_credentials_and_redirect_secrets():
    headers = redact_headers(
        {
            "Authorization": "Bearer secret",
            "Cookie": "sid=secret",
            "X-Request-ID": "safe-id",
        }
    )
    assert headers["Authorization"] == REDACTED
    assert headers["Cookie"] == REDACTED
    assert headers["X-Request-ID"] == "safe-id"

    safe = redact_url(
        "https://user:pass@example.test/disclosures/a.pdf?token=secret&year=2025"
    )
    assert "user" not in safe and "pass" not in safe and "secret" not in safe
    assert "%5BREDACTED%5D" in safe and "year=2025" in safe
    chain = redact_redirect_chain((safe, "https://example.test/disclosures/b?api_key=x"))
    assert all("api_key=x" not in item for item in chain)


def test_redirect_chain_is_checked_before_next_hop():
    target = validate_redirect(
        "https://example.test/disclosures/start",
        "/disclosures/next",
        _policy(),
        redirect_count=0,
        resolved_addresses=("93.184.216.34",),
    )
    assert target.host == "example.test"

    with pytest.raises(SecurityPolicyError) as error:
        validate_redirect(
            "https://example.test/disclosures/start",
            "https://evil.test/steal",
            _policy(),
            redirect_count=0,
            resolved_addresses=("93.184.216.34",),
        )
    assert error.value.reason_code == "redirect_not_allowlisted"


@pytest.mark.parametrize(
    "url,address",
    [
        ("http://example.test/disclosures/a", "93.184.216.34"),
        ("https://user@example.test/disclosures/a", "93.184.216.34"),
        ("https://example.test/disclosures/a", "127.0.0.1"),
        ("https://example.test/disclosures/a", "169.254.1.1"),
    ],
)
def test_https_downgrade_credentials_and_private_target_fail_closed(url, address):
    with pytest.raises(SecurityPolicyError) as error:
        validate_transport_target(url, _policy(), resolved_addresses=(address,))
    assert error.value.reason_code == "transport_target_forbidden"


def test_content_length_limit_stops_before_body_iteration():
    limits = ResponseLimits(max_compressed_bytes=3, max_decompressed_bytes=10)
    assert validate_content_length("3", limits) == 3
    with pytest.raises(ResponseSizeExceeded) as error:
        validate_content_length("4", limits)
    assert error.value.reason_code == "response_size_exceeded"


def test_chunked_overrun_is_rejected_without_returning_partial_body():
    limits = ResponseLimits(max_compressed_bytes=5, max_decompressed_bytes=20)
    with pytest.raises(ResponseSizeExceeded):
        read_limited_body([b"123", b"456"], limits)


def test_decompression_limit_is_applied_to_decoded_bytes():
    compressed = gzip.compress(b"A" * 100)
    limits = ResponseLimits(
        max_compressed_bytes=len(compressed) + 1,
        max_decompressed_bytes=50,
    )
    with pytest.raises(ResponseSizeExceeded):
        read_limited_body([compressed], limits, content_encoding="gzip")


def test_restricted_response_diagnostic_keeps_only_minimum_metadata():
    body = b'<html><form><input type="password"></form></html>'
    diagnostic = minimal_response_diagnostic(
        status_code=200,
        headers={"Content-Type": "text/html", "Set-Cookie": "session=secret"},
        body=body,
        expected_mime_types=("application/pdf",),
    )
    assert diagnostic.classification_hint == "login_required"
    assert diagnostic.headers["Set-Cookie"] == REDACTED
    assert diagnostic.body_length == len(body)
    assert len(diagnostic.body_sha256) == 64
    assert not hasattr(diagnostic, "body")


def test_orphan_scan_is_read_only_and_does_not_auto_delete(tmp_path):
    store = ContentAddressedBlobStore(tmp_path / "data", "namespace-security")
    archived = store.archive_bytes(b"uncommitted-after-metadata-failure")

    first = store.scan_orphans(())
    repeated = store.scan_orphans(())

    assert first == repeated
    assert [item.relative_path for item in first] == [archived.relative_path]
    assert store.resolve_blob(archived.relative_path).read_bytes() == (
        b"uncommitted-after-metadata-failure"
    )
