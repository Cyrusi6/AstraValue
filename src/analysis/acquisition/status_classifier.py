from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import httpx

from .source_gate import LocalSourceGateTimeout
from .models import AcquisitionOutcome


ACQUISITION_OUTCOMES = tuple(AcquisitionOutcome)


@dataclass(frozen=True)
class AttemptClassification:
    outcome: AcquisitionOutcome
    reason_code: str
    retryable: bool = False

    def __post_init__(self) -> None:
        try:
            normalized = AcquisitionOutcome(self.outcome)
        except ValueError as exc:
            raise ValueError(f"unknown acquisition outcome: {self.outcome}") from exc
        object.__setattr__(self, "outcome", normalized)
        if not self.reason_code:
            raise ValueError("reason_code is required")


_LOGIN_MARKERS = (
    b"type=\"password\"",
    b"name=\"password\"",
    b"/login",
    "登录".encode("utf-8"),
)
_PAYWALL_MARKERS = (
    b"paywall",
    b"subscribe to continue",
    "付费".encode("utf-8"),
    "订阅后".encode("utf-8"),
)
_CHALLENGE_MARKERS = (
    b"captcha",
    b"cf-chl-",
    b"challenge-platform",
    b"access denied",
    "验证码".encode("utf-8"),
    "访问验证".encode("utf-8"),
)
_POLICY_REASONS = {
    "redirect_not_allowlisted",
    "transport_target_forbidden",
    "response_size_exceeded",
    "license_not_approved",
    "manual_access_review_required",
    "retention_replay_conflict",
    "research_scope_context_missing",
    "research_scope_body_not_selected",
}


def classify_exception(
    error: BaseException,
    *,
    has_committed_segments: bool = False,
    deadline_was_exceeded: bool = False,
) -> AttemptClassification:
    if isinstance(error, LocalSourceGateTimeout):
        return _partial_or(
            has_committed_segments,
            "rate_limited",
            LocalSourceGateTimeout.reason_code,
            retryable=True,
        )
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        # A timeout outcome is only valid when the executing request actually
        # crossed its recorded deadline.  A crashed process is closed as
        # abandoned by lease recovery instead of being routed through here.
        if not deadline_was_exceeded:
            return _partial_or(
                has_committed_segments,
                "network_failed",
                "transport_interrupted_before_deadline",
                retryable=True,
            )
        return _partial_or(
            has_committed_segments,
            "timeout",
            "request_deadline_exceeded",
            retryable=True,
        )
    if isinstance(error, (httpx.NetworkError, ConnectionError, OSError)):
        return _partial_or(
            has_committed_segments,
            "network_failed",
            "transport_error",
            retryable=True,
        )
    if isinstance(error, (ValueError, UnicodeError, KeyError, TypeError)):
        return _partial_or(
            has_committed_segments,
            "parse_failed",
            "response_parse_failed",
        )
    return _partial_or(
        has_committed_segments,
        "network_failed",
        "unexpected_transport_error",
    )


def classify_response(
    *,
    status_code: int,
    headers: Mapping[str, str] | None = None,
    body_prefix: bytes = b"",
    expected_mime: tuple[str, ...] = (),
    schema_valid: bool | None = None,
    has_committed_segments: bool = False,
    policy_reason: str | None = None,
) -> AttemptClassification:
    normalized_headers = {
        str(k).strip().lower(): str(v).strip() for k, v in (headers or {}).items()
    }
    if policy_reason is not None:
        if policy_reason not in _POLICY_REASONS:
            raise ValueError(f"unknown policy reason: {policy_reason}")
        return _partial_or(
            has_committed_segments,
            "policy_skipped",
            policy_reason,
        )
    if status_code == 429:
        return _partial_or(
            has_committed_segments,
            "rate_limited",
            "upstream_rate_limited",
            retryable=True,
        )
    if status_code == 401:
        return _partial_or(has_committed_segments, "login_required", "http_401")
    if status_code == 402:
        return _partial_or(has_committed_segments, "paywalled", "http_402")
    if status_code == 403:
        return _partial_or(has_committed_segments, "restricted", "http_403")
    if status_code in {408, 504}:
        return _partial_or(
            has_committed_segments,
            "timeout",
            f"http_{status_code}",
            retryable=True,
        )
    if status_code >= 500:
        return _partial_or(
            has_committed_segments,
            "network_failed",
            f"http_{status_code}",
            retryable=True,
        )
    if status_code >= 400:
        return _partial_or(
            has_committed_segments,
            "network_failed",
            f"http_{status_code}",
        )

    # An exact upstream signal takes precedence over body/MIME/schema checks.
    # Unknown values must retain their normal parsing outcome.
    if normalized_headers.get("x-tengine-error", "").lower() == "denied by bot":
        return _partial_or(
            has_committed_segments, "restricted", "upstream_bot_challenge"
        )

    lowered = body_prefix[:8192].lower()
    if any(marker in lowered for marker in _PAYWALL_MARKERS):
        return _partial_or(has_committed_segments, "paywalled", "paywall_detected")
    if any(marker in lowered for marker in _LOGIN_MARKERS):
        return _partial_or(
            has_committed_segments,
            "login_required",
            "login_page_detected",
        )
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return _partial_or(
            has_committed_segments,
            "restricted",
            "challenge_page_detected",
        )

    content_type = normalized_headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if expected_mime and not any(
        content_type == item.lower() or content_type.startswith(f"{item.lower()}+")
        for item in expected_mime
    ):
        return _partial_or(
            has_committed_segments,
            "parse_failed",
            "unexpected_mime",
        )
    if schema_valid is False:
        return _partial_or(
            has_committed_segments,
            "parse_failed",
            "schema_validation_failed",
        )
    return AttemptClassification("success", "protocol_success")


def classify_discovery_completion(
    *,
    normalized_resource_count: int,
    schema_valid: bool,
    proof_complete: bool,
    pagination_terminal: bool,
    declared_total: int | None,
    normalized_total: int,
    unchanged_anchor_valid: bool = False,
    has_committed_segments: bool = False,
) -> AttemptClassification:
    if unchanged_anchor_valid:
        return AttemptClassification("unchanged", "valid_discovery_anchor")
    totals_close = declared_total is None or declared_total == normalized_total
    if not (schema_valid and proof_complete and pagination_terminal and totals_close):
        reason = (
            "schema_validation_failed"
            if not schema_valid
            else "discovery_terminal_proof_missing"
        )
        return _partial_or(has_committed_segments, "parse_failed", reason)
    if normalized_resource_count < 0 or normalized_total < 0:
        return _partial_or(
            has_committed_segments,
            "parse_failed",
            "negative_discovery_count",
        )
    if normalized_resource_count == 0:
        # An empty body/list is not evidence of no disclosure.  no_data is
        # reserved for a successful, terminal query whose upstream total
        # explicitly closes at zero.
        if declared_total != 0:
            return _partial_or(
                has_committed_segments,
                "parse_failed",
                "discovery_terminal_proof_missing",
            )
        return AttemptClassification("no_data", "validated_empty_result")
    return AttemptClassification("success", "validated_nonempty_result")


def classify_fetch_result(
    *,
    status_code: int,
    existing_snapshot_valid: bool,
    response_sha256: str | None = None,
    existing_sha256: str | None = None,
) -> tuple[AttemptClassification, str | None]:
    if status_code == 304:
        if not existing_snapshot_valid:
            return AttemptClassification(
                "parse_failed", "validator_anchor_missing"
            ), None
        return AttemptClassification("unchanged", "valid_304_anchor"), "unchanged"
    if not response_sha256:
        return AttemptClassification("parse_failed", "content_hash_missing"), None
    if existing_sha256 and response_sha256 == existing_sha256:
        return AttemptClassification("unchanged", "content_hash_unchanged"), "unchanged"
    disposition = "changed" if existing_sha256 else "new"
    return AttemptClassification("success", f"content_{disposition}"), disposition


def _partial_or(
    has_committed_segments: bool,
    outcome: str,
    reason_code: str,
    retryable: bool = False,
) -> AttemptClassification:
    if has_committed_segments:
        return AttemptClassification(
            "partial_success",
            reason_code,
            retryable=retryable,
        )
    return AttemptClassification(outcome, reason_code, retryable=retryable)
