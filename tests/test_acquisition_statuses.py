from __future__ import annotations

import httpx
import pytest

from analysis.acquisition.status_classifier import (
    ACQUISITION_OUTCOMES,
    AttemptClassification,
    classify_discovery_completion,
    classify_exception,
    classify_fetch_result,
    classify_response,
)


def test_exactly_twelve_outcomes_and_abandoned_is_not_one() -> None:
    assert len(ACQUISITION_OUTCOMES) == 12
    assert len(set(ACQUISITION_OUTCOMES)) == 12
    assert "abandoned" not in ACQUISITION_OUTCOMES


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"status_code": 200}, "success"),
        ({"status_code": 401}, "login_required"),
        ({"status_code": 402}, "paywalled"),
        ({"status_code": 403}, "restricted"),
        ({"status_code": 429}, "rate_limited"),
        ({"status_code": 504}, "timeout"),
        ({"status_code": 503}, "network_failed"),
        (
            {
                "status_code": 200,
                "headers": {"Content-Type": "text/html"},
                "expected_mime": ("application/pdf",),
            },
            "parse_failed",
        ),
        (
            {"status_code": 200, "policy_reason": "redirect_not_allowlisted"},
            "policy_skipped",
        ),
        (
            {
                "status_code": 200,
                "headers": {"Content-Type": "text/html"},
                "body_prefix": b'<form action="/login"><input type="password">',
            },
            "login_required",
        ),
        (
            {
                "status_code": 200,
                "headers": {"Content-Type": "text/html"},
                "body_prefix": b"CAPTCHA challenge-platform",
            },
            "restricted",
        ),
    ],
)
def test_http_response_has_typed_outcome(kwargs: dict, expected: str) -> None:
    assert classify_response(**kwargs).outcome == expected


def test_first_schema_failure_is_parse_failed_later_page_is_partial_success() -> None:
    first = classify_response(status_code=200, schema_valid=False)
    later = classify_response(
        status_code=200,
        schema_valid=False,
        has_committed_segments=True,
    )
    assert first == AttemptClassification("parse_failed", "schema_validation_failed")
    assert later.outcome == "partial_success"


def test_local_policy_failure_is_not_restricted() -> None:
    classification = classify_response(
        status_code=200,
        policy_reason="transport_target_forbidden",
    )
    assert classification.outcome == "policy_skipped"
    assert classification.outcome != "restricted"


@pytest.mark.parametrize(
    "reason",
    ["research_scope_context_missing", "research_scope_body_not_selected"],
)
def test_research_scope_policy_failures_are_recordable(reason: str) -> None:
    classification = classify_response(status_code=200, policy_reason=reason)
    assert classification == AttemptClassification("policy_skipped", reason)


def test_no_data_requires_schema_total_pages_and_terminal_proof() -> None:
    valid = classify_discovery_completion(
        normalized_resource_count=0,
        schema_valid=True,
        proof_complete=True,
        pagination_terminal=True,
        declared_total=0,
        normalized_total=0,
    )
    assert valid.outcome == "no_data"
    for field in ("schema_valid", "proof_complete", "pagination_terminal"):
        kwargs = {
            "normalized_resource_count": 0,
            "schema_valid": True,
            "proof_complete": True,
            "pagination_terminal": True,
            "declared_total": 0,
            "normalized_total": 0,
        }
        kwargs[field] = False
        assert classify_discovery_completion(**kwargs).outcome == "parse_failed"
    assert classify_discovery_completion(
        normalized_resource_count=0,
        schema_valid=True,
        proof_complete=True,
        pagination_terminal=True,
        declared_total=None,
        normalized_total=0,
    ).outcome == "parse_failed"


def test_invalid_304_never_becomes_unchanged() -> None:
    invalid, disposition = classify_fetch_result(
        status_code=304,
        existing_snapshot_valid=False,
    )
    valid, valid_disposition = classify_fetch_result(
        status_code=304,
        existing_snapshot_valid=True,
    )
    assert invalid.outcome == "parse_failed"
    assert invalid.reason_code == "validator_anchor_missing"
    assert disposition is None
    assert valid.outcome == "unchanged"
    assert valid_disposition == "unchanged"


def test_hash_is_final_fetch_version_decision() -> None:
    unchanged, unchanged_disposition = classify_fetch_result(
        status_code=200,
        existing_snapshot_valid=True,
        response_sha256="a" * 64,
        existing_sha256="a" * 64,
    )
    changed, changed_disposition = classify_fetch_result(
        status_code=200,
        existing_snapshot_valid=True,
        response_sha256="b" * 64,
        existing_sha256="a" * 64,
    )
    assert (unchanged.outcome, unchanged_disposition) == ("unchanged", "unchanged")
    assert (changed.outcome, changed_disposition) == ("success", "changed")


def test_timeout_is_not_fabricated_from_an_interruption() -> None:
    error = httpx.ReadTimeout("read")
    interrupted = classify_exception(error, deadline_was_exceeded=False)
    timed_out = classify_exception(error, deadline_was_exceeded=True)
    assert interrupted.outcome == "network_failed"
    assert timed_out.outcome == "timeout"


@pytest.mark.parametrize("name,value", [
    ("x-tengine-error", "denied by bot"),
    (" X-TENGINE-ERROR ", " DENIED BY BOT \t"),
])
def test_tengine_challenge_header_precedes_body_mime_and_schema(name, value):
    result = classify_response(
        status_code=200, headers={name: value, "Content-Type": "text/html"},
        body_prefix=b'<input type="password">',
        expected_mime=("application/pdf",), schema_valid=False,
    )
    assert result == AttemptClassification("restricted", "upstream_bot_challenge")


@pytest.mark.parametrize("name,value", [
    ("x-tengine-error", "denied by bots"),
    ("x-tengine-error", "not denied by bot"),
    ("x-unknown-error", "denied by bot"),
    ("x-tengine-error", ""),
])
def test_unknown_challenge_header_keeps_unexpected_mime(name, value):
    assert classify_response(
        status_code=200, headers={name: value, "Content-Type": "text/html"},
        body_prefix=b"<html>ordinary page</html>", expected_mime=("application/pdf",),
    ) == AttemptClassification("parse_failed", "unexpected_mime")


@pytest.mark.parametrize("status,outcome,reason", [
    (429, "rate_limited", "upstream_rate_limited"),
    (403, "restricted", "http_403"),
    (504, "timeout", "http_504"),
])
def test_http_status_precedes_tengine_challenge_header(status, outcome, reason):
    result = classify_response(
        status_code=status, headers={"x-tengine-error": "denied by bot"},
        expected_mime=("application/pdf",),
    )
    assert (result.outcome, result.reason_code) == (outcome, reason)


def test_later_page_tengine_challenge_keeps_partial_cause():
    result = classify_response(
        status_code=200, headers={"x-tengine-error": "denied by bot"},
        has_committed_segments=True,
    )
    assert result == AttemptClassification("partial_success", "upstream_bot_challenge")
